"""已核验的个人场馆协议；读取与单次写入分离，默认禁止真实订单。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import time
from typing import Any, Mapping
from uuid import UUID

import httpx

from services.collector.leyu_protocol import LeyuError, MAX_RESPONSE_BYTES, ODDS_SCALE, SUCCESS_CODES, decode_envelope, parse_line, timestamp
from .account_session import AccountScope, VenueSession

AMOUNT_PATH = "/yewu12/api/user/amount"
ORDERS_PATH = "/yewu13/v1/betOrder/client/getOrderListV4PB"
LATEST_MARKET_PATH = "/yewu13/v1/betOrder/client/queryLatestMarketInfo"
LIMITS_PATH = "/yewu13/v1/betOrder/client/queryMarketMaxMinBetMoney"
BET_PATH = "/yewu13/v1/betOrder/client/bet"


class SubmissionUnknown(LeyuError):
    """写请求可能到达上游，必须保留敞口并查单，禁止自动重发。"""


class SubmissionRejected(LeyuError):
    pass


def number(value: Any, *, minimum: Decimal | None = None) -> Decimal:
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError("not scalar")
        result = Decimal(str(value))
        if not result.is_finite() or abs(result) > Decimal("1000000000000") or (minimum is not None and result < minimum):
            raise ValueError("out of range")
        return result
    except (InvalidOperation, ValueError):
        raise LeyuError("invalid_native_number") from None


def _optional_number(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return str(number(value))
    except LeyuError:
        return None


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    return next((row[key] for key in keys if row.get(key) not in (None, "")), None)


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise LeyuError("invalid_native_rows")
    return value


@dataclass(frozen=True)
class NativeSelection:
    match_id: str
    market_instance_id: str
    play_id: str
    selection_id: str
    selection_code: str
    sport_id: str
    chpid: str
    kind: str
    period: str
    line: Decimal
    price: Decimal
    phase: str
    score_benchmark: str = ""

    def __post_init__(self):
        for value in (self.match_id, self.market_instance_id, self.play_id, self.selection_id, self.chpid):
            if not isinstance(value, str) or not value or len(value) > 160 or "\n" in value or "\r" in value:
                raise LeyuError("invalid_native_selection")
        if (self.kind not in {"winner", "handicap", "total"} or self.period not in {"full_time", "first_half", "second_half"}
                or self.selection_code not in {"1", "2", "X", "Over", "Under"} or not self.sport_id.isdigit()):
            raise LeyuError("unsupported_native_selection")
        allowed = {"winner": {"1", "2", "X"}, "handicap": {"1", "2"}, "total": {"Over", "Under"}}
        if self.selection_code not in allowed[self.kind]:
            raise LeyuError("native_selection_direction_mismatch")
        line, price = number(self.line), number(self.price, minimum=Decimal("1.00001"))
        if price * ODDS_SCALE != (price * ODDS_SCALE).to_integral_value():
            raise LeyuError("unsupported_native_price_precision")
        object.__setattr__(self, "line", line)
        object.__setattr__(self, "price", price)


@dataclass(frozen=True, repr=False)
class PreparedBet:
    scope: AccountScope
    payload_json: str
    digest: str
    expires_monotonic: float
    checked_at: str

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)


@dataclass(frozen=True)
class SubmissionPermit:
    scope: AccountScope
    execution_id: str
    prepared_digest: str

    def __post_init__(self):
        try:
            UUID(self.execution_id)
        except (ValueError, TypeError, AttributeError):
            raise LeyuError("invalid_execution_identity") from None


def normalize_order(row: Mapping[str, Any], state: str) -> dict[str, Any]:
    details = row.get("detailList")
    details = details if isinstance(details, list) else []
    detail = details[0] if len(details) == 1 and isinstance(details[0], Mapping) else {}
    merged = {**detail, **{key: value for key, value in row.items() if value not in (None, "")}}
    parent_id = _first(row, "orderNo", "orderNumber", "orderId")
    detail_id = _first(merged, "betNo", "bet_no")
    identifier = parent_id or detail_id
    if not identifier or isinstance(identifier, bool):
        raise LeyuError("native_order_identifier_missing")
    stake = _optional_number(_first(merged, "betAmount", "orderAmountTotal", "amount", "stake", "betMoney"))
    payout = _optional_number(_first(merged, "backAmount", "payout"))
    raw_profit = _first(merged, "profitAmount", "profit")
    profit = _optional_number(raw_profit)
    basis = "provider_reported_net" if profit is not None else "unknown"
    if raw_profit is None and stake is not None and payout is not None:
        profit, basis = str(Decimal(payout) - Decimal(stake)), "gross_return_minus_stake"
    return {"order_no": str(identifier), "identifier_kind": "order" if parent_id else "detail",
            "state": state, "stake": stake, "payout": payout, "net_profit": profit, "pnl_basis": basis, "fees": None,
            "price": _optional_number(_first(merged, "oddFinally", "oddsFinally", "odds", "odd")),
            "placed_at": timestamp(_first(merged, "betTime", "createTime", "orderTime")),
            "settled_at": timestamp(_first(merged, "settleTime", "settled_at", "settleAt", "settlementTime")),
            "match_id": str(_first(merged, "matchId", "eventId", "mid") or "") or None,
            "market_name": _first(merged, "playNameCn", "marketName", "playName"),
            "selection_name": _first(merged, "playOptionNameCn", "optionName", "playOptionsName", "playOptionName"),
            "result": _first(merged, "result", "betResult", "settleResult", "winStatus")}


def order_page(payload: Any, state: str, page: int, size: int) -> dict[str, Any]:
    if not isinstance(payload, (Mapping, list)):
        raise LeyuError("invalid_native_order_page")
    count = None
    records = None
    if isinstance(payload, list):
        records = payload
    else:
        for key in ("totalRecord", "totalCount", "recordCount", "count", "total"):
            if key in payload:
                value = payload[key]
                if not isinstance(value, bool) and str(value).isdigit():
                    count = int(value)
                    break
        for key in ("records", "list", "betRecords", "gameList", "game_record_list", "gameRecordList"):
            if isinstance(payload.get(key), list):
                records = payload[key]
                break
        if records is None and isinstance(payload.get("data"), (Mapping, list)):
            nested = order_page(payload["data"], state, page, size)
            if count is not None:
                nested["total"] = count
                nested["has_more"] = count > page * size
            return nested
    if records is None:
        raise LeyuError("native_order_records_missing")
    flattened = []
    malformed = 0
    for row in records:
        if not isinstance(row, Mapping):
            malformed += 1
            continue
        children = row.get("data")
        flattened.extend(children if isinstance(children, list) else [row])
    items = []
    seen = {}
    conflicts = set()
    for row in flattened:
        try:
            if not isinstance(row, Mapping):
                raise LeyuError("invalid_native_order")
            item = normalize_order(row, state)
            key = item["identifier_kind"], item["order_no"]
            if key in seen:
                if seen[key] != item:
                    conflicts.add(key)
                raise LeyuError("duplicate_native_order")
            seen[key] = item
            items.append(item)
        except LeyuError:
            malformed += 1
    if count is not None and count < len(items):
        malformed += 1
    return {"items": items, "state": state, "page": page, "size": size, "total": count,
            "has_more": count > page * size if count is not None else len(flattened) >= size,
            "malformed_records": malformed, "conflicting_order_ids": [value for kind, value in conflicts if kind == "order"],
            "absence_proves_rejection": False}


class AccountProtocolClient:
    def __init__(self, scope: AccountScope, session: VenueSession, *, transport=None, live_orders_enabled: bool = False):
        if scope != session.scope:
            raise LeyuError("account_session_scope_mismatch")
        self.scope, self.session = scope, session
        self.live_orders_enabled = live_orders_enabled
        self._used_permits: set[str] = set()
        self._issued_prepared: dict[str, tuple[str, float]] = {}
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3), limits=httpx.Limits(max_connections=3, max_keepalive_connections=3),
                                      transport=transport, trust_env=False, follow_redirects=False,
                                      headers={"requestId": session.request_id, "Lang": "zh", "Accept-Language": "zh-CN,zh;q=0.9",
                                               "clientVersionType": "4", "Origin": session.origin, "Referer": session.origin + "/",
                                               "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36",
                                               "Accept": "application/json, text/plain, */*"})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self):
        await self.http.aclose()

    async def _request(self, path: str, body: Mapping[str, Any] | None = None, *, submission: bool = False) -> Any:
        try:
            async with asyncio.timeout(12), self.http.stream("POST" if body is not None else "GET", self.session.base_url + path,
                                                          **({"json": dict(body)} if body is not None else {})) as response:
                if response.status_code >= 300:
                    raise LeyuError("account_http_rejected", str(response.status_code))
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise LeyuError("account_response_too_large")
                try:
                    payload = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise LeyuError("invalid_account_response") from None
                return payload if submission else decode_envelope(payload) if isinstance(payload, Mapping) and "code" in payload else payload
        except (httpx.HTTPError, TimeoutError):
            raise LeyuError("account_transport_failure") from None

    async def balance(self) -> dict[str, Any]:
        payload = await self._request(AMOUNT_PATH)
        if not isinstance(payload, Mapping):
            raise LeyuError("account_balance_unavailable")
        candidates = [number(payload[key], minimum=Decimal("0")) for key in ("amount", "balance", "availableAmount", "availableBalance")
                      if payload.get(key) not in (None, "")]
        if not candidates or len(set(candidates)) != 1:
            raise LeyuError("account_balance_unavailable")
        return {"available": str(candidates[0]), "currency": "CNY", "observed_at": datetime.now(timezone.utc).isoformat()}

    async def orders(self, state: str, *, page: int = 1, size: int = 100, begin: datetime, end: datetime) -> dict[str, Any]:
        if (state not in {"settled", "unsettled"} or not 1 <= page <= 1000000 or not 1 <= size <= 100
                or begin.tzinfo is None or end.tzinfo is None or begin >= end):
            raise LeyuError("invalid_account_order_query")
        body = {"orderStatus": 0 if state == "unsettled" else 1, "timeType": 0, "page": page, "size": size,
                "beginTime": int(begin.timestamp() * 1000), "endTime": int(end.timestamp() * 1000), "outright": 0}
        return order_page(await self._request(ORDERS_PATH, body), state, page, size)

    async def lookup(self, order_no: str, *, begin: datetime, end: datetime, max_pages: int = 10) -> dict[str, Any]:
        try:
            async with asyncio.timeout(60):
                return await self._lookup(order_no, begin=begin, end=end, max_pages=max_pages)
        except TimeoutError:
            raise LeyuError("account_lookup_deadline_exceeded") from None

    async def _lookup(self, order_no: str, *, begin: datetime, end: datetime, max_pages: int) -> dict[str, Any]:
        if not order_no or len(order_no) > 160 or not 1 <= max_pages <= 100:
            raise LeyuError("invalid_account_order_lookup")
        matches = []
        complete = True
        ambiguous = False
        for state in ("unsettled", "settled"):
            previous_ids = set()
            for page in range(1, max_pages + 1):
                response = await self.orders(state, page=page, begin=begin, end=end)
                if response["total"] is None:
                    complete = False
                ambiguous |= order_no in response["conflicting_order_ids"]
                identities = {(item["identifier_kind"], item["order_no"]) for item in response["items"]}
                if response["malformed_records"] or (page > 1 and identities and identities <= previous_ids):
                    complete = False
                previous_ids |= identities
                matches.extend(item for item in response["items"] if item["identifier_kind"] == "order" and item["order_no"] == order_no)
                if not response["has_more"]:
                    break
                if page == max_pages:
                    complete = False
        # 缺失不能证明拒单；结算回执优于同订单的短暂未结缓存。
        unique = {item["state"]: item for item in matches}
        ambiguous |= any(any(previous != item for previous in matches if previous["state"] == item["state"]) for item in matches)
        return {"found": bool(unique) and not ambiguous, "receipt": None if ambiguous else unique.get("settled") or unique.get("unsettled"),
                "scan_complete": complete and not ambiguous, "ambiguous": ambiguous, "absence_proves_rejection": False}

    async def preflight(self, selection: NativeSelection, stake: Decimal, *, device_imei: str = "") -> PreparedBet:
        if selection.phase != "live":
            raise LeyuError("prematch_execution_protocol_unverified")
        amount = number(stake, minimum=Decimal("0.01"))
        if amount != amount.quantize(Decimal("0.01")):
            raise LeyuError("invalid_account_stake_precision")
        latest_query = {"idList": [{"marketId": selection.market_instance_id, "matchInfoId": selection.match_id,
                        "oddsId": selection.selection_id, "oddsType": selection.selection_code, "playId": selection.play_id,
                        "chpid": selection.chpid, "matchType": 2, "sportId": int(selection.sport_id)}]}
        limit_detail = {"marketId": selection.market_instance_id, "matchId": selection.match_id, "playId": selection.play_id,
                        "playOptionId": selection.selection_id, "oddsValue": str(int(selection.price * ODDS_SCALE)), "matchType": 2, "deviceType": 3}
        tasks = [asyncio.create_task(self._request(LATEST_MARKET_PATH, latest_query)),
                 asyncio.create_task(self._request(LIMITS_PATH, {"orderMaxBetMoney": [limit_detail]})),
                 asyncio.create_task(self.balance())]
        try:
            latest, limits, wallet = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        markets = [row for row in _rows(latest) if str(row.get("id")) == selection.market_instance_id
                   and str(row.get("matchInfoId")) == selection.match_id and str(row.get("playId")) == selection.play_id]
        if len(markets) != 1:
            raise LeyuError("selected_native_market_not_unique")
        market = markets[0]
        if (str(market.get("matchStatus")) != "1" or str(market.get("matchOver", "0")) != "0"
                or str(market.get("matchHandicapStatus")) != "0" or str(market.get("status")) != "0"):
            raise LeyuError("selected_native_market_not_open")
        options = [row for row in _rows(market.get("marketOddsList")) if str(row.get("id")) == selection.selection_id]
        if len(options) != 1 or str(options[0].get("oddsType")) != selection.selection_code or str(options[0].get("oddsStatus")) not in {"0", "1"}:
            raise LeyuError("selected_native_option_not_open")
        option = options[0]
        price = number(option.get("oddsValue")) / ODDS_SCALE
        raw_line = market.get("marketValue")
        line = Decimal("0") if selection.kind == "winner" and raw_line in (None, "") else parse_line(raw_line)
        if price != selection.price or line != selection.line:
            raise LeyuError("selected_native_quote_changed")
        matching_limits = [row for row in _rows(limits) if str(row.get("playOptionsId")) == selection.selection_id]
        if len(matching_limits) != 1:
            raise LeyuError("native_limits_not_unique")
        limit = matching_limits[0]
        if (str(limit.get("code")) not in SUCCESS_CODES
                or (limit.get("playId") not in (None, "") and str(limit["playId"]) != selection.play_id)
                or limit.get("type") not in (None, "", "1", 1)):
            raise LeyuError("native_limits_rejected")
        minimum, maximum = number(limit.get("minBet"), minimum=Decimal("0")), number(limit.get("orderMaxPay"), minimum=Decimal("0.01"))
        if not minimum <= amount <= maximum or Decimal(wallet["available"]) < amount:
            raise LeyuError("account_stake_outside_available_limits")
        detail = {"betAmount": format(amount, ".2f"), "matchId": selection.match_id, "marketId": selection.market_instance_id,
                  "playId": selection.play_id, "playOptions": selection.selection_code, "playOptionsId": selection.selection_id,
                  "oddFinally": str(price), "odds": str(int(price * ODDS_SCALE)), "marketValue": "" if raw_line is None else str(raw_line),
                  "marketTypeFinally": "EU", "matchType": 2, "sportId": selection.sport_id, "scoreBenchmark": selection.score_benchmark}
        if market.get("placeNum") is not None:
            detail["placeNum"] = market["placeNum"]
        payload = {"acceptOdds": 2, "currencyCode": "CNY", "deviceImei": device_imei, "deviceType": "3", "openMiltSingle": 0,
                   "preBet": "0", "seriesOrders": [{"seriesType": "1", "seriesSum": 1, "fullBet": 0, "orderDetailList": [detail]}]}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256((self.scope.user_id + ":" + self.scope.account_id + ":" + str(self.scope.revision) + ":" + encoded).encode()).hexdigest()
        expires = time.monotonic() + 15
        self._issued_prepared = {key: value for key, value in self._issued_prepared.items() if value[1] > time.monotonic()}
        if len(self._issued_prepared) >= 128:
            raise LeyuError("prepared_order_budget_exceeded")
        self._issued_prepared[digest] = encoded, expires
        return PreparedBet(self.scope, encoded, digest, expires, datetime.now(timezone.utc).isoformat())

    async def submit_once(self, prepared: PreparedBet, permit: SubmissionPermit) -> dict[str, Any]:
        if not self.live_orders_enabled:
            raise LeyuError("live_orders_disabled")
        if (prepared.scope != self.scope or permit.scope != self.scope or permit.prepared_digest != prepared.digest
                or prepared.expires_monotonic <= time.monotonic()
                or self._issued_prepared.get(prepared.digest) != (prepared.payload_json, prepared.expires_monotonic)):
            raise LeyuError("execution_permit_invalid_or_expired")
        if permit.execution_id in self._used_permits:
            raise LeyuError("execution_already_sent")
        self._used_permits.add(permit.execution_id)
        self._issued_prepared.pop(prepared.digest, None)
        try:
            payload = await self._request(BET_PATH, prepared.payload, submission=True)
        except LeyuError:
            raise SubmissionUnknown("native_submission_unknown") from None
        if not isinstance(payload, Mapping) or "code" not in payload:
            raise SubmissionUnknown("native_submission_receipt_missing")
        code = str(payload["code"])
        if not re.fullmatch(r"[0-9]{1,12}", code):
            raise SubmissionUnknown("native_submission_business_code_unknown")
        if code not in SUCCESS_CODES:
            raise SubmissionRejected("native_submission_rejected", code)
        data = payload.get("data")
        orders = data.get("orderDetailRespList") if isinstance(data, Mapping) else None
        if not isinstance(orders, list) or len(orders) != 1 or not isinstance(orders[0], Mapping):
            raise SubmissionUnknown("native_submission_order_missing")
        order = orders[0]
        state, order_no = str(order.get("orderStatusCode")), str(order.get("orderNo") or "")
        if state == "0":
            raise SubmissionRejected("native_submission_explicitly_rejected", code)
        if state not in {"1", "2"} or not order_no or len(order_no) > 160:
            raise SubmissionUnknown("native_submission_state_unknown")
        return {"state": "accepted" if state == "1" else "pending", "order_no": order_no,
                "provider_status": state, "provider_code": code, "execution_id": permit.execution_id}
