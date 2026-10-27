"""乐鱼授权样本契约：只还原事实，不生成时间、价格或推荐。"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import io
import gzip
import json
import re
import zlib
from typing import Any, Mapping

SUCCESS_CODES = {"0", "200", "0000000"}
AUTH_CODES = {"0401013", "0401014"}
PERMISSION_CODES = {"0401015"}
RATE_LIMIT_CODES = {"0401038", "0400429"}
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_DECODED_BYTES = 64 * 1024 * 1024
ODDS_SCALE = Decimal("100000")
SPORTS = {"1": "football", "2": "basketball", "5": "tennis", "7": "baseball", "10": "badminton", "4": "ice_hockey"}
MARKETS = {
    "1": ("winner", "full_time", "全场独赢"),
    "17": ("winner", "first_half", "上半场独赢"),
    "4": ("handicap", "full_time", "全场让球"),
    "19": ("handicap", "first_half", "上半场让球"),
    "2": ("total", "full_time", "全场大小"),
    "18": ("total", "first_half", "上半场大小"),
}


class LeyuError(RuntimeError):
    """只携带脱敏类别及短业务码，不包含响应、URL 或凭据。"""

    def __init__(self, category: str, code: str = "", *, operation: str = "", http_status: int | None = None):
        self.category = category
        self.code = code if re.fullmatch(r"[0-9]{1,12}", code) else ""
        self.operation = operation if operation in {"fixture_snapshot", "odds_batch", "odds_single"} else ""
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        super().__init__(category + (f" code={self.code}" if self.code else ""))


class AuthExpired(LeyuError):
    pass


class RateLimited(LeyuError):
    def __init__(self, category: str, code: str = "", *, retry_after_seconds: int | None = None, **kwargs):
        super().__init__(category, code, **kwargs)
        self.retry_after_seconds = (min(900, max(0, retry_after_seconds))
                                    if type(retry_after_seconds) is int else None)


def objects(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, list):
        return [row for row in value if isinstance(row, Mapping)]
    return []


def decode_payload(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if len(value) > MAX_RESPONSE_BYTES * 2:
        raise LeyuError("encoded_response_too_large")
    try:
        # 授权抓包中的长 base64 含换行；只移除空白，保留严格字母表检查。
        compact = "".join(value.split())
        raw = base64.b64decode(compact, validate=True)
    except (ValueError, binascii.Error):
        raw = b""
    if raw.startswith(b"\x1f\x8b"):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                raw = stream.read(MAX_DECODED_BYTES + 1)
            if len(raw) > MAX_DECODED_BYTES:
                raise LeyuError("decoded_response_too_large")
            return json.loads(raw)
        except (OSError, EOFError, ValueError, UnicodeError, zlib.error):
            raise LeyuError("invalid_compressed_payload") from None
    stripped = value.strip()
    if stripped.startswith(("{", "[")):
        try:
            return json.loads(stripped)
        except ValueError:
            raise LeyuError("invalid_json_payload") from None
    return value


def decode_envelope(payload: Any) -> Any:
    if not isinstance(payload, Mapping):
        raise LeyuError("invalid_response_envelope")
    code = str(payload.get("code", ""))
    if code and code not in SUCCESS_CODES:
        if code in AUTH_CODES:
            raise AuthExpired("session_expired", code)
        if code in RATE_LIMIT_CODES:
            raise RateLimited("upstream_rate_limited", code)
        raise LeyuError("permission_denied" if code in PERMISSION_CODES else "upstream_rejected", code)
    return decode_payload(payload.get("data", payload))


def timestamp(value: Any) -> str | None:
    """明确无效/未知为 None；绝不回退 now。"""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat() if value.tzinfo else None
    if value in (None, "", 0, "0") or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
        except (ValueError, TypeError):
            return None
    if not number.is_finite() or number <= 0:
        return None
    if number > Decimal("100000000000"):
        number /= 1000
    try:
        return datetime.fromtimestamp(float(number), timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def parse_line(value: Any) -> Decimal | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    chunks = text.split("/")
    if len(chunks) not in {1, 2} or any(not part.strip() for part in chunks):
        return None
    try:
        # 上游负分盘可用 -0.5/1 表达 -0.5/-1，第二份继承首份负号。
        parts = [Decimal(part.strip()) for part in chunks]
        if len(parts) == 2 and chunks[0].strip().startswith("-") and not chunks[1].strip().startswith(("-", "+")):
            parts[1] = -abs(parts[1])
        if not all(part.is_finite() for part in parts):
            return None
        line = sum(parts) / len(parts)
        return line if abs(line) <= 1000 else None
    except InvalidOperation:
        return None


def score(value: Any, period: str = "S1") -> tuple[int | None, int | None]:
    chunks = value if isinstance(value, list) else str(value or "").split(",")
    for chunk in chunks:
        chunk = str(chunk).strip().strip("'")
        if "|" in chunk:
            key, chunk = chunk.split("|", 1)
            if key != period:
                continue
        elif period != "S1":
            continue
        found = re.fullmatch(r"(\d+)\s*[:-]\s*(\d+)", chunk)
        if found:
            return int(found[1]), int(found[2])
    return None, None


def clock_minute(raw: Mapping[str, Any], phase: str) -> int | None:
    if phase != "live" or str(raw.get("mmp", "")) == "0":
        return None
    text = str(raw.get("mst", "")).strip()
    if re.fullmatch(r"\d{1,3}:\d{2}", text):
        minute, second = map(int, text.split(":"))
        return minute if second < 60 and minute <= 200 else None
    # 实测 mst 为已过秒数（例如 1564），mmp 为阶段编码。
    if text.isdigit():
        minute = int(text) // 60
        return minute if minute <= 200 else None
    return None


def phase_for(raw: Mapping[str, Any]) -> str:
    status = str(raw.get("ms", raw.get("status", "")))
    period = str(raw.get("mmp", ""))
    if status == "3" or period == "999" or status == "finished":
        return "finished"
    if status in {"cancelled", "canceled"}:
        return "cancelled"
    if status in {"1", "live"}:
        return "live"
    if status in {"0", "scheduled"}:
        return "scheduled"
    # 未核验的上游状态不映射为可交易阶段。
    return "cancelled"


def fixture_rows(payload: Any) -> tuple[list[Mapping[str, Any]], Mapping[str, Any]]:
    if isinstance(payload, list):
        return objects(payload), {}
    if not isinstance(payload, Mapping):
        raise LeyuError("invalid_fixture_payload")
    for key in ("matchsList", "matches", "items", "data"):
        if isinstance(payload.get(key), list):
            return objects(payload[key]), payload
    if payload.get("mid"):
        return [payload], {}
    raise LeyuError("fixture_rows_missing")


def parse_fixtures(payload: Any, source_id: str = "leyu", *, received_at: datetime | None = None) -> list[dict[str, Any]]:
    rows, root = fixture_rows(payload)
    leagues = {str(row.get("tid")): row for row in objects(root.get("tids_obj", []))}
    sports = {str(row.get("csid")): row for row in objects(root.get("spList", []))}
    received = timestamp(received_at or datetime.now(timezone.utc))
    result = []
    for raw in rows:
        mid = str(raw.get("mid") or raw.get("id") or "").strip()
        home = str(raw.get("mhn") or raw.get("homeTeam") or raw.get("home") or "").strip()
        away = str(raw.get("man") or raw.get("awayTeam") or raw.get("away") or "").strip()
        kickoff = timestamp(raw.get("mgt") or raw.get("kickoffAt"))
        if not mid or not home or not away or not kickoff:
            continue
        sport_id = str(raw.get("csid") or "")
        league_id = str(raw.get("tid") or "")
        league = leagues.get(league_id, {})
        phase = phase_for(raw)
        home_score, away_score = score(raw.get("msc") or raw.get("score"))
        half_home, half_away = score(raw.get("msc"), "S2")
        source_time = timestamp(raw.get("source_updated_at") or raw.get("ctsp"))
        result.append({
            "id": f"{source_id}:{mid}", "source_id": f"{source_id}:{mid}",
            "source_match_id": mid, "sport": SPORTS.get(sport_id, f"source_{sport_id or 'unknown'}"),
            "sport_name": str(raw.get("csna") or sports.get(sport_id, {}).get("csna") or ""),
            "competition_id": league_id or None,
            "competition_name": str(raw.get("tn") or raw.get("tnjc") or league.get("tn") or "未知联赛"),
            "home_team_id": str(raw.get("mhid") or "") or None, "home_team": home, "home_aliases": [],
            "away_team_id": str(raw.get("maid") or "") or None, "away_team": away, "away_aliases": [],
            "kickoff_at": kickoff, "phase": phase, "match_state": str(raw.get("mmp") or raw.get("ms") or ""),
            "home_score": home_score, "away_score": away_score,
            "half_home_score": half_home, "half_away_score": half_away,
            "clock_minute": clock_minute(raw, phase), "clock_raw": str(raw.get("mst") or ""),
            "source_updated_at": source_time, "source_time_quality": "known" if source_time else "unknown",
            "received_at": received,
        })
    return result


def _market_rows(payload: Any, fixture_id: str) -> list[Mapping[str, Any]]:
    if isinstance(payload, Mapping) and ("hpsData" in payload or "hps" in payload):
        return [payload]
    if isinstance(payload, Mapping):
        payload = payload.get("data", [])
    wanted = fixture_id.rsplit(":", 1)[-1]
    return [row for row in objects(payload) if not row.get("mid") or str(row.get("mid")) == wanted]


def _quote_status(option: Mapping[str, Any], line: Mapping[str, Any], block: Mapping[str, Any]) -> str:
    # HTTP 样本 hs=0 为开放，os=1 为选项可用；其他未核验编码保守停盘。
    if str(option.get("os", "1")) in {"3", "closed"} or str(line.get("hs", "0")) in {"2", "3", "closed"}:
        return "closed"
    if str(option.get("os", "")) not in {"1", "active"} or str(line.get("hs", "")) not in {"0", "active"}:
        return "suspended"
    if str(block.get("status", "active")) != "active":
        return "suspended"
    return "active"


def parse_quotes(payload: Any, fixture_id: str, updated_at: datetime | None = None) -> list[dict[str, Any]]:
    """updated_at 仅作为接收时间；源时间必须来自 ctsp。"""
    received = timestamp(updated_at or datetime.now(timezone.utc))
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in _market_rows(payload, fixture_id):
        definitions = {str(item.get("chpid")): item for item in objects(row.get("hpsPns", []))}
        containers = objects(row.get("hpsData", [])) or [row]
        for container in containers:
            for key in ("hps", "hpsAdd"):
                for block in objects(container.get(key, [])):
                    market_id = str(block.get("chpid") or block.get("marketId") or "")
                    if not market_id:
                        continue
                    definition = definitions.get(market_id, {})
                    kind, period, default_name = MARKETS.get(market_id, ("unsupported", f"source_{market_id}", market_id))
                    name = str(definition.get("hpn") or block.get("hpn") or default_name)
                    for line in objects(block.get("hl", [])):
                        raw_line = line.get("hv", line.get("line"))
                        numeric_line = Decimal("0") if kind == "winner" and raw_line in (None, "") else parse_line(raw_line)
                        if numeric_line is None:
                            continue
                        for option in objects(line.get("ol", [])):
                            selection_raw = str(option.get("ot") or "")
                            selection = {"1": "home", "2": "away", "X": "draw", "Over": "over", "Under": "under"}.get(selection_raw)
                            if not selection:
                                continue
                            try:
                                price = Decimal(str(option.get("ov", ""))) / ODDS_SCALE
                                if not price.is_finite() or price <= 1:
                                    price = None
                            except InvalidOperation:
                                price = None
                            source_time = timestamp(option.get("ctsp") or block.get("ctsp"))
                            status = _quote_status(option, line, block)
                            if price is None or kind == "unsupported" or not option.get("oid") or not line.get("hid"):
                                status = "suspended"
                            item = {
                                "market_id": market_id, "market_name": name, "market_kind": kind, "kind": kind,
                                "period": period, "line": str(numeric_line), "selection": selection,
                                # 只有明确赛前才可用整期比分；滚球基线需规则证据后单独标定。
                                "score_basis": "full_period" if str(row.get("ms", "")) in {"0", "scheduled"} else "unknown",
                                "price": str(price) if price is not None else None,
                                "source_updated_at": source_time, "received_at": received,
                                "source_time_quality": "known" if source_time else "unknown", "status": status,
                                "play_id": str(block.get("hpid") or definition.get("hpid") or market_id),
                                "market_instance_id": str(line.get("hid") or ""),
                                "selection_id": str(option.get("oid") or ""), "is_main": key == "hps",
                            }
                            identity = market_id, str(numeric_line), selection
                            prior = result.get(identity)
                            if prior is None or (source_time or "") > (prior["source_updated_at"] or ""):
                                result[identity] = item
    return list(result.values())
