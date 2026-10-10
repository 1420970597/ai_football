#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Account telemetry and explicit YBTY single-order transport.

The centre wallet exposes ``venue/getBalance`` and ``allBalance``.  Sports
orders themselves live behind the YBTY business gateway opened by
``venue/launch``: ``/yewu12/api/user/amount`` and the compressed
``/yewu13/v1/betOrder/client/getOrderListV4PB`` endpoint.  The older
``record/betRecord*`` routes remain a compatibility fallback only.  Response
schemas vary slightly between gateway versions, so this module extracts
well-known scalar/list fields and never invents a missing value.
"""
from __future__ import annotations

import json
import gzip
import math
import http.client
import ssl
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from typing import Any, Dict, Mapping, Optional, Sequence

from .leyu_app_login import AppLoginSessionProvider, login_provider_from_env
from .leyu_app_session import AppSessionBootstrapper, bootstrapper_from_env
from .leyu_client import SUCCESS_CODES, OV_SCALE, decode_envelope
from .session import SessionError

BALANCE_PATH = "/game/api/v1/venue/getBalance"
ALL_BALANCE_PATH = "/game/api/v1/venue/allBalance"
BET_LIST_PATH = "/game/api/v1/record/betRecordList"
BET_TOTAL_PATH = "/game/api/v1/record/betRecordTotal"

# YBTY's own business gateway is separate from the wallet gateway.  These
# paths are used by the Android sports order screen and require the dynamic
# requestId returned by venue/launch.
VENUE_AMOUNT_PATH = "/yewu12/api/user/amount"
VENUE_ORDER_PATH = "/yewu13/v1/betOrder/client/getOrderListV4PB"
VENUE_PREBET_ORDER_PATH = "/yewurecord/v1/betOrder/client/getH5PreBetOrderList"
VENUE_LATEST_MARKET_PATH = "/yewu13/v1/betOrder/client/queryLatestMarketInfo"
VENUE_LIMIT_PATH = "/yewu13/v1/betOrder/client/queryMarketMaxMinBetMoney"
VENUE_BET_PATH = "/yewu13/v1/betOrder/client/bet"


class BetSubmissionUnknown(SessionError):
    """A write may have reached the provider. Never replay it automatically."""


class BetSubmissionRejected(SessionError):
    """The provider explicitly rejected a submitted order."""


def _bet_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SessionError("场馆未返回有效的" + label) from exc
    if isinstance(value, bool) or not math.isfinite(number):
        raise SessionError("场馆未返回有效的" + label)
    return number


def _bet_rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(row, Mapping) for row in value):
        raise SessionError("投注前置接口响应结构异常")
    return value


def _walk(value: Any) -> Sequence[Any]:
    if isinstance(value, Mapping):
        return list(value.values())
    if isinstance(value, list):
        return value
    return []


def _find_number(value: Any, names: set[str]) -> Optional[float]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in names:
                try:
                    return float(item)
                except (TypeError, ValueError):
                    pass
        for item in value.values():
            found = _find_number(item, names)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_number(item, names)
            if found is not None:
                return found
    return None


def _find_records(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in {"list", "records", "betrecords", "gamelist", "game_record_list", "gamerecordlist"} and isinstance(item, list):
                rows: list[Mapping[str, Any]] = []
                for group in item:
                    if not isinstance(group, Mapping):
                        continue
                    # The native response groups records by day and puts the
                    # actual bet rows below ``data``.
                    children = group.get("data")
                    if isinstance(children, list):
                        day = group.get("day")
                        for row in children:
                            if isinstance(row, Mapping):
                                record = dict(row)
                                if day is not None and "day" not in record:
                                    record["day"] = day
                                rows.append(record)
                    else:
                        rows.append(group)
                return rows
        for item in value.values():
            found = _find_records(item)
            if found:
                return found
    elif isinstance(value, list):
        if all(isinstance(row, Mapping) for row in value):
            return list(value)
    return []


def _record_count(value: Any) -> Optional[int]:
    """Read the count fields used by the App's PageEntity/TotalLine DTOs."""
    if isinstance(value, Mapping):
        for key in ("totalRecord", "totalCount", "recordCount", "count", "total"):
            item = value.get(key)
            if isinstance(item, bool):
                continue
            try:
                if item is not None:
                    return int(item)
            except (TypeError, ValueError):
                pass
        total_line = value.get("totalLine")
        if isinstance(total_line, Mapping):
            for key in ("countId", "count", "totalRecord"):
                item = total_line.get(key)
                try:
                    if item is not None:
                        return int(item)
                except (TypeError, ValueError):
                    pass
        for item in value.values():
            found = _record_count(item)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _record_count(item)
            if found is not None:
                return found
    return None


def _venue_balances(value: Any) -> list[Mapping[str, Any]]:
    """Extract the wallet application's venue balance rows.

    ``allBalance`` returns a list under ``data`` on the current App gateway,
    while older gateways have wrapped it in another ``list``/``records`` key.
    Keep the raw row fields so callers can diagnose a venue mapping without
    treating a missing venue as a zero balance.
    """
    if isinstance(value, Mapping):
        data = value.get("data")
        if isinstance(data, list):
            return [row for row in data if isinstance(row, Mapping)]
        for key, item in value.items():
            if str(key).lower() in {"list", "records", "balances", "venuebalances"}:
                rows = _venue_balances(item)
                if rows:
                    return rows
        for item in value.values():
            rows = _venue_balances(item)
            if rows:
                return rows
    elif isinstance(value, list):
        return [row for row in value if isinstance(row, Mapping)]
    return []


def _venue_amount(row: Mapping[str, Any]) -> Optional[float]:
    for key in ("amount", "balance", "amountV", "availableBalance"):
        if key in row:
            try:
                return float(row[key])
            except (TypeError, ValueError):
                continue
    return None


def _first_value(row: Mapping[str, Any], names: Sequence[str]) -> Any:
    """Return the first non-empty alias from a provider order row."""
    for name in names:
        value = row.get(name)
        if value is not None and value != "":
            return value
    return None


def _as_optional_float(value: Any) -> Optional[float]:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def _items_amount(items: Sequence[Mapping[str, Any]]) -> float:
    return round(sum(float(item.get("amount") or 0.0) for item in items), 2)


def _normalise_venue_record(row: Mapping[str, Any], status: str) -> Dict[str, Any]:
    """Map the changing YBTY protobuf/JSON DTO to the account API contract."""
    item = dict(row)
    details = row.get("detailList")
    detail = details[0] if isinstance(details, list) and details and isinstance(details[0], Mapping) else {}
    # Order rows keep money/status at the parent level and put the actual
    # match/market selection in detailList.  Merge only for alias lookup so
    # parent identifiers remain authoritative.
    merged: Dict[str, Any] = dict(detail)
    merged.update({key: value for key, value in row.items() if value not in (None, "")})
    for key, value in detail.items():
        if merged.get(key) in (None, ""):
            merged[key] = value
    item["source"] = "leyu_ybty"
    item["status"] = status
    item["order_no"] = _first_value(merged, ("orderNo", "orderNumber", "orderId", "id"))
    item["match"] = _first_value(merged, ("matchInfo", "match", "matchNameCn", "eventName", "matchName"))
    item["league"] = _first_value(merged, ("leagueName", "tournamentName", "matchName"))
    item["match_id"] = _first_value(merged, ("matchId", "eventId", "mid"))
    item["home"] = _first_value(merged, ("homeName", "homeTeamName", "home", "matchHomeName"))
    item["away"] = _first_value(merged, ("awayName", "awayTeamName", "away", "matchAwayName"))
    item["market"] = _first_value(merged, ("marketName", "market", "playName", "playNameCn", "marketTypeName"))
    item["option"] = _first_value(merged, ("optionName", "playOptionsName", "playOptionName", "option", "outcome"))
    item["outcome"] = item["option"]
    item["odds"] = _as_optional_float(_first_value(merged, ("odds", "oddFinally", "oddsFinally", "odd")))
    item["amount"] = _as_optional_float(_first_value(merged, ("betAmount", "orderAmountTotal", "amount", "stake", "betMoney")))
    item["profit"] = _as_optional_float(_first_value(merged, ("profit", "profitAmount", "winAmount", "payout")))
    item["score"] = _first_value(detail, ("settleScore", "scoreBenchmark", "matchScore", "score", "比分")) or _first_value(merged, ("score", "settleScore", "scoreBenchmark", "matchScore", "比分"))
    item["result"] = _first_value(detail, ("result", "betResult", "settleResult", "winStatus")) or _first_value(row, ("result", "betResult", "settleResult", "winStatus"))
    item["details"] = [dict(entry) for entry in details if isinstance(entry, Mapping)] if isinstance(details, list) else []
    return item


class LeyuAccountClient:
    """Reuse venue authentication; write calls are explicit and never retried."""

    def __init__(self, bootstrapper: AppSessionBootstrapper,
                 login_provider: Optional[AppLoginSessionProvider] = None,
                 timeout: float = 12.0) -> None:
        self.bootstrapper = bootstrapper
        self.login_provider = login_provider
        self.timeout = timeout
        self._venue_session: Any = None

    def _refresh_token(self) -> bool:
        if self.login_provider is None:
            return False
        token = self.login_provider.refresh_token()
        if not token:
            return False
        self.bootstrapper.credentials.token = token
        return True

    def _headers(self) -> Dict[str, str]:
        """Build the native account headers from the captured App request.

        The account/wallet endpoints use the main Android client route.  It
        differs from the ``sport_android`` route used by ``venue/launch`` and
        also echoes the current token as ``x-user-ency``.
        """
        if self.bootstrapper.signer is not None:
            self.bootstrapper.signer.initialize(
                self.bootstrapper.app_host, self.bootstrapper.credentials.uuid, self.timeout)
        headers = {
            "x-api-client": "android",
            "x-api-version": "2.0.1",
            "x-api-site": "2001",
            "x-api-language": "CHS",
            "x-api-uuid": self.bootstrapper.credentials.uuid,
            "x-api-token": self.bootstrapper.credentials.token,
            "x-api-currency": "CNY",
            "content-type": "application/json; charset=utf-8",
            "user-agent": "okhttp/4.12.0",
            "accept-encoding": "identity",
            "x-user-ency": self.bootstrapper.credentials.token,
        }
        if self.bootstrapper.signer is not None:
            headers.update(self.bootstrapper.signer.headers("/game/api"))
        elif self.bootstrapper.signature:
            headers["x-api-xxx"] = self.bootstrapper.signature
        return headers

    def _post(self, path: str, body: Mapping[str, Any], _auth_retry: bool = False) -> Mapping[str, Any]:
        req = urllib.request.Request(
            self.bootstrapper.app_host.rstrip("/") + path,
            data=json.dumps(dict(body), separators=(",", ":")).encode(), method="POST")
        for key, value in self._headers().items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout,
                                        context=ssl._create_unverified_context()) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as exc:
            raise SessionError("账户接口请求失败（%s）" % type(exc).__name__) from exc
        if not isinstance(payload, Mapping):
            raise SessionError("账户接口响应结构异常")
        code = payload.get("status_code")
        if code not in (None, 6000, "6000"):
            if (not _auth_retry and str(code) in {"6001", "0401013"}
                    and self._refresh_token()):
                return self._post(path, body, _auth_retry=True)
            message = str(payload.get("message") or payload.get("msg") or "").strip()
            detail = (": " + message) if message else ""
            raise SessionError("账户接口返回业务码 %s%s" % (code, detail))
        return payload

    @staticmethod
    def _record_query(flag: int, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        """Build the same record filter sent by SportOrderMineViewModel.

        The API rejects omitted dates with 6008.  The App defaults to the
        last 30 calendar days, inclusive, and uses sport gameType ``1``.
        """
        today = date.today()
        start = today - timedelta(days=29)
        return {
            "endAt": today.isoformat() + " 23:59:59",
            "startAt": start.isoformat() + " 00:00:00",
            "flag": int(flag),
            "page": int(page),
            "pageSize": int(page_size),
            "queryFlag": 1,
            "venueId": 0,
            "gameType": 1,
            "nationalityCode": "CN",
            "currency": "CNY",
        }

    def _fetch_records(self, flag: int) -> Dict[str, Any]:
        body = self._record_query(flag)
        total: Mapping[str, Any] = {}
        total_error: Optional[str] = None
        try:
            total = self._post(BET_TOTAL_PATH, body)
        except SessionError as exc:
            # A summary failure must not hide the actual rows.  The native
            # App requests total and list independently as well.
            total_error = str(exc)
        listing = self._post(BET_LIST_PATH, body)
        rows = []
        for row in _find_records(listing):
            item = dict(row)
            # RecordData.flag is also 0/1, but use the request flag as the
            # authoritative state when older gateway versions omit it.
            item.setdefault("flag", flag)
            item["status"] = "unsettled" if flag == 0 else "settled"
            rows.append(item)
        count = _record_count(total)
        if count is None:
            count = _record_count(listing)
        result = {"count": count if count is not None else len(rows),
                "items": rows,
                "page": (listing.get("data", {}).get("page")
                         if isinstance(listing.get("data"), Mapping) else listing.get("page"))
                         if isinstance(listing, Mapping) else None,
                "page_num": (listing.get("data", {}).get("pageNum")
                              if isinstance(listing.get("data"), Mapping) else listing.get("pageNum"))
                              if isinstance(listing, Mapping) else None,
                "page_size": (listing.get("data", {}).get("pageSize")
                               if isinstance(listing.get("data"), Mapping) else listing.get("pageSize"))
                               if isinstance(listing, Mapping) else None}
        if total_error:
            result["error"] = total_error
        return result

    def _acquire_venue_session(self) -> Any:
        """Acquire the short-lived requestId session used by YBTY."""
        current = self._venue_session
        if current is not None and not getattr(current, "expired", False):
            return current
        provider = self.login_provider
        if provider is not None and hasattr(provider, "acquire"):
            current = provider.acquire(previous=current)
        elif hasattr(self.bootstrapper, "acquire"):
            current = self.bootstrapper.acquire()
        else:
            raise SessionError("未配置乐鱼体育场馆会话引导器")
        self._venue_session = current
        return current

    def _venue_headers(self, session: Any) -> Dict[str, str]:
        origin = str(getattr(session, "origin", "") or "").rstrip("/")
        if not origin:
            origin = str(getattr(session, "host", "") or self.bootstrapper.app_host).rstrip("/")
        return {
            "requestId": str(getattr(session, "request_id", "")),
            "Lang": "zh-CN",
            "clientVersionType": "4",
            "Origin": origin,
            "Referer": origin + "/",
            "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "Accept-Encoding": "gzip, deflate",
        }

    def _venue_request(self, path: str, body: Optional[Mapping[str, Any]] = None,
                       *, decode: bool = False, _auth_retry: bool = False) -> Any:
        """Call a YBTY business endpoint and optionally decode its gzip envelope."""
        session = self._acquire_venue_session()
        host = str(getattr(session, "host", "") or "").rstrip("/")
        if not host:
            raise SessionError("乐鱼体育场馆会话缺少业务网关")
        method = "POST" if body is not None else "GET"
        data = (json.dumps(dict(body), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                if body is not None else None)
        req = urllib.request.Request(host + path, data=data, method=method)
        for key, value in self._venue_headers(session).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout,
                                        context=ssl._create_unverified_context()) as response:
                raw = response.read()
                if str(response.headers.get("Content-Encoding", "")).lower() == "gzip":
                    raw = gzip.decompress(raw)
                payload = json.loads(raw.decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            if path != VENUE_BET_PATH and not _auth_retry and exc.code in (401, 403):
                self._venue_session = None
                self._acquire_venue_session()
                return self._venue_request(path, body, decode=decode, _auth_retry=True)
            raise SessionError("乐鱼体育场馆接口请求失败（HTTP %s）" % exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException) as exc:
            raise SessionError("乐鱼体育场馆接口请求失败（%s）" % type(exc).__name__) from exc
        if not isinstance(payload, Mapping):
            raise SessionError("乐鱼体育场馆接口响应结构异常")
        if path == VENUE_BET_PATH:
            # Preserve the business envelope for explicit receipt validation.
            # Unlike reads, a debit request must never refresh and replay.
            return payload
        try:
            # Amount is a plain JSON endpoint on some gateways, while other
            # versions still wrap it in the same ``code/data`` envelope as
            # the compressed order endpoint.  Decode either form.
            result = decode_envelope(payload) if (decode or "code" in payload) else payload
        except Exception as exc:
            text = str(exc)
            if (not _auth_retry and any(token in text for token in ("0401013", "0401014", "0401015", "过期"))):
                self._venue_session = None
                self._acquire_venue_session()
                return self._venue_request(path, body, decode=decode, _auth_retry=True)
            raise SessionError("乐鱼体育场馆接口返回失败") from exc
        return result

    def prepare_bet(self, detail: Mapping[str, Any], stake: float) -> Dict[str, Any]:
        """Recheck the selected live market, native odds, limits and wallet."""
        query = {"idList": [{
            "marketId": detail["marketId"], "matchInfoId": detail["matchId"],
            "oddsId": detail["playOptionsId"], "oddsType": detail["playOptions"],
            "playId": detail["playId"], "chpid": detail.get("chpid", detail["playId"]),
            "matchType": detail["matchType"], "sportId": int(detail["sportId"]),
            **({"placeNum": detail["placeNum"]} if "placeNum" in detail else {}),
        }]}
        rows = _bet_rows(self._venue_request(VENUE_LATEST_MARKET_PATH, query))
        market = next((row for row in rows if str(row.get("id")) == str(detail["marketId"])
                       and str(row.get("matchInfoId")) == str(detail["matchId"])
                       and str(row.get("playId")) == str(detail["playId"])), None)
        if market is None:
            raise SessionError("最新盘口中找不到推荐的比赛与盘口")
        if (str(market.get("matchStatus")) != "1" or str(market.get("matchOver", 0)) != "0"
                or str(market.get("matchHandicapStatus")) != "0" or str(market.get("status")) != "0"):
            raise SessionError("最新比赛未进行或盘口已关闭/暂停")
        options = _bet_rows(market.get("marketOddsList"))
        option = next((row for row in options if str(row.get("id")) == str(detail["playOptionsId"])), None)
        if option is None or str(option.get("oddsStatus")) not in ("0", "1"):
            raise SessionError("投注选项已关闭或不存在")
        if str(option.get("oddsType")) != str(detail["playOptions"]):
            raise SessionError("投注选项方向已变化")
        raw_odds = _bet_number(option.get("oddsValue"), "最新赔率")
        odds = raw_odds / OV_SCALE
        if odds <= 1 or not math.isclose(odds, _bet_number(detail["oddFinally"], "推荐赔率"), abs_tol=0.000005):
            raise SessionError("赔率已变化，等待新综合推荐重新估值")
        if str(market.get("marketValue") or "") != str(detail.get("marketValue") or ""):
            raise SessionError("盘口线已变化，等待新综合推荐")
        fresh = {**detail, "oddFinally": str(odds), "odds": str(round(raw_odds)), "matchType": 2}
        if market.get("placeNum") is not None:
            fresh["placeNum"] = market["placeNum"]
        limit_detail = {
            "marketId": fresh["marketId"], "matchId": fresh["matchId"],
            "playId": fresh["playId"], "playOptionId": fresh["playOptionsId"],
            "oddsValue": fresh["odds"], "matchType": 2, "deviceType": 3,
        }
        limits = _bet_rows(self._venue_request(VENUE_LIMIT_PATH, {"orderMaxBetMoney": [limit_detail]}))
        # The captured Android single-bet response identifies the option but
        # explicitly leaves playId/type empty. Empty optional echoes are not
        # contradictions; nonempty echoes must still match the requested bet.
        matching = [row for row in limits
                    if str(row.get("playOptionsId")) == str(fresh["playOptionsId"])]
        if len(matching) != 1:
            raise SessionError("场馆未返回该单关的有效投注限额")
        limit = matching[0]
        if ((limit.get("playId") not in (None, "")
             and str(limit["playId"]) != str(fresh["playId"]))
                or (limit.get("type") not in (None, "") and str(limit["type"]) != "1")
                or str(limit.get("code")) not in SUCCESS_CODES):
            raise SessionError("场馆未返回该单关的有效投注限额")
        minimum = _bet_number(limit.get("minBet"), "最小投注额")
        maximum = _bet_number(limit.get("orderMaxPay"), "最大投注额")
        if minimum < 0 or maximum <= 0 or not minimum <= stake <= maximum:
            raise SessionError("投注额不在场馆限额内（%g~%g）" % (minimum, maximum))
        amount = self._venue_request(VENUE_AMOUNT_PATH)
        balance = _find_number(amount, {"amount", "balance", "availableamount", "availablebalance"})
        if balance is None or not math.isfinite(balance) or balance < stake:
            raise SessionError("体育场馆余额不足或不可读取")
        return fresh

    def submit_bet(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        """Send exactly one debit request and report accepted/pending/rejected."""
        try:
            response = self._venue_request(VENUE_BET_PATH, payload)
        except SessionError as exc:
            raise BetSubmissionUnknown("提交结果未知，请核对场馆注单；系统不会自动重发") from exc
        if not isinstance(response, Mapping) or "code" not in response:
            raise BetSubmissionUnknown("下单回执缺少业务码，需核对场馆注单")
        code = str(response["code"])
        if code not in SUCCESS_CODES:
            raise BetSubmissionRejected("场馆拒单，业务码 " + code)
        data = response.get("data")
        if not isinstance(data, Mapping):
            raise BetSubmissionUnknown("下单回执缺少订单数据，需核对场馆注单")
        rows = data.get("orderDetailRespList")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise BetSubmissionUnknown("单关回执缺少唯一订单，需核对场馆注单")
        order = rows[0]
        status = str(order.get("orderStatusCode"))
        if status == "0":
            raise BetSubmissionRejected("场馆明确拒单")
        if not order.get("orderNo") or status not in ("1", "2"):
            raise BetSubmissionUnknown("下单回执缺少订单号或状态，需核对场馆注单")
        return {"status": "accepted" if status == "1" else "pending",
                "submitted": True, "order_no": str(order["orderNo"]),
                "provider_status": int(status), "provider_code": code}

    def _fetch_venue_records(self, flag: int) -> Dict[str, Any]:
        """Fetch one settled state from the native YBTY order endpoint."""
        body = {
            "orderStatus": int(flag), "timeType": 0, "page": 1, "size": 100,
            "beginTime": 0, "endTime": 9999999999999, "outright": 0,
        }
        decoded = self._venue_request(VENUE_ORDER_PATH, body, decode=True)
        rows = [_normalise_venue_record(row, "unsettled" if flag == 0 else "settled")
                for row in _find_records(decoded)]
        count = _record_count(decoded)
        return {
            "count": count if count is not None else len(rows),
            "amount": _items_amount(rows),
            "items": rows,
            "status": "unsettled" if flag == 0 else "settled",
            "source": "leyu_ybty",
        }

    def _fetch_venue_account(self) -> Dict[str, Any]:
        """Read YBTY balance and both order states in one launched session."""
        amount_payload = self._venue_request(VENUE_AMOUNT_PATH)
        amount = _find_number(amount_payload, {"amount", "balance", "availableamount", "availablebalance"})
        output: Dict[str, Any] = {"sports_balance": amount, "source": "leyu_ybty"}
        errors: list[str] = []
        for flag, key in ((0, "unsettled"), (1, "settled")):
            try:
                output[key] = self._fetch_venue_records(flag)
            except SessionError as exc:
                output[key] = {"count": None, "items": [], "status": key, "source": "leyu_ybty"}
                errors.append(str(exc))
        if errors:
            output["error"] = "; ".join(errors)
        return output

    def fetch(self) -> Dict[str, Any]:
        fetched_at = time.time()
        # This is the exact body used by the Android wallet screen.  Sending
        # ``enName`` (the venue-launch body) is rejected with business code
        # 6008 even when the account token is valid.
        balance_payload = self._post(BALANCE_PATH, {"withLockMoney": 1})
        center_balance = _find_number(balance_payload, {
            "balance", "availablebalance", "accountbalance", "ybtybalance",
            "centerwalletbalance", "walletbalance",
        })
        venue_rows: list[Mapping[str, Any]] = []
        venue_error = ""
        try:
            venue_rows = _venue_balances(self._post(ALL_BALANCE_PATH, {}))
        except SessionError as exc:
            venue_error = str(exc)
        sports_balance: Optional[float] = None
        for row in venue_rows:
            name = str(row.get("enName") or row.get("channelCode") or "").upper()
            if name == "YBTY":
                sports_balance = _venue_amount(row)
                break
        # ``balance`` is kept for clients of the original response contract,
        # but now prefers the actual sports venue wallet when available.
        balance = sports_balance if sports_balance is not None else center_balance
        unsettled: Dict[str, Any] = {"count": None, "items": []}
        settled: Dict[str, Any] = {"count": None, "items": []}
        errors: list[str] = []
        if venue_error:
            errors.append(venue_error)
        venue_data: Optional[Dict[str, Any]] = None
        try:
            venue_data = self._fetch_venue_account()
            sports_balance = venue_data.get("sports_balance") if venue_data.get("sports_balance") is not None else sports_balance
            if isinstance(venue_data.get("unsettled"), Mapping):
                unsettled.update(venue_data["unsettled"])
            if isinstance(venue_data.get("settled"), Mapping):
                settled.update(venue_data["settled"])
            if venue_data.get("error"):
                errors.append(str(venue_data["error"]))
        except SessionError as exc:
            # Keep the centre-wallet response useful when the venue launch or
            # endpoint is temporarily unavailable, but make the degradation
            # visible to the API/UI instead of reporting empty orders as truth.
            errors.append(str(exc))

        # Compatibility fallback for deployments that only have the old App
        # wallet APIs configured.  A successful YBTY response always wins.
        if venue_data is None:
            for flag, target in ((0, unsettled), (1, settled)):
                try:
                    record_data = self._fetch_records(flag)
                    target.update(record_data)
                    if record_data.get("error"):
                        errors.append(str(record_data["error"]))
                except SessionError as exc:
                    errors.append(str(exc))
        balance = sports_balance if sports_balance is not None else center_balance
        return {"available": (sports_balance is not None or center_balance is not None
                               or bool(unsettled["items"] or settled["items"])),
                "source": "leyu_ybty" if venue_data is not None else "leyu_app",
                "currency": "CNY", "balance": balance,
                "center_balance": center_balance,
                "sports_balance": sports_balance,
                "venue_balances": [dict(row) for row in venue_rows],
                "unsettled": unsettled, "settled": settled,
                "fetched_at": fetched_at, "error": "; ".join(errors) or None}


def account_client_from_env(env: Optional[Mapping[str, str]] = None) -> Optional[LeyuAccountClient]:
    values = env if env is not None else __import__("os").environ
    bootstrapper = bootstrapper_from_env(values)
    if bootstrapper is None:
        return None
    # Keep the refreshed App token across the short-lived HTTP client objects
    # created by the API handler.  The cache is already used by the collector
    # session chain and is protected with mode 0600 by AppLoginSessionProvider.
    cache_hint = (values.get("LEYU_SESSION_CACHE") or "").strip()
    token_cache = ""
    if cache_hint:
        import os
        token_cache = os.path.join(os.path.dirname(cache_hint) or ".", "_app_token.json")
    login = login_provider_from_env(values, token_cache_path=token_cache or None)
    if login is not None:
        # The persisted token is account-scoped.  After an account switch
        # there is no matching cache entry, so authenticate once before any
        # read-only account request; otherwise the old env token could expose
        # the previous account's balance.
        token = login.cached_token or login.refresh_token()
        if token:
            bootstrapper.credentials.token = token
    return LeyuAccountClient(bootstrapper, login_provider=login)
