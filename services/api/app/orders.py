"""用户账户事务锁下的订单预留与发送许可；未知结果始终保留敞口。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import uuid

from fastapi import HTTPException

from .auth import audit
from .membership import has_entitlement
from .review_state import fresh_quote
from .security import decrypt_secret
from .runtime_settings import runtime_quote_max_age


def live_enabled(settings) -> bool:
    return getattr(settings, "live_orders_enabled", False) is True


async def global_enabled(conn, settings) -> bool:
    value = await conn.fetchval("SELECT encrypted_value FROM v2_admin_settings WHERE setting_key='auto_betting_enabled'")
    if not value:
        return False
    try:
        return decrypt_secret(settings.encryption_key, value).casefold() == "true"
    except Exception:
        return False


async def invalid_policies(conn):
    """Select only invalid armed policies, locking in account then policy order."""
    return await conn.fetch(
        "SELECT a.user_id FROM v2_leyu_accounts a JOIN v2_users u ON u.id=a.user_id "
        "JOIN v2_user_bet_settings b ON b.user_id=a.user_id WHERE b.auto_enabled AND ("
        "u.disabled_at IS NOT NULL OR u.role<>'user' OR a.health_state<>'healthy' OR "
        "a.renewal_state IN('unknown','rejected') OR a.encrypted_session IS NULL OR "
        "b.consent_account_id IS DISTINCT FROM a.id OR b.consent_binding_revision IS DISTINCT FROM a.binding_revision OR "
        "NOT EXISTS(SELECT 1 FROM v2_memberships m JOIN v2_membership_plans p ON p.id=m.plan_id "
        "WHERE m.user_id=a.user_id AND m.status='active' AND p.enabled AND m.starts_at<=now() "
        "AND (m.ends_at IS NULL OR m.ends_at>now()) AND p.entitlements @> '{\"automatic_betting\":true}'::jsonb)) "
        "ORDER BY a.updated_at FOR UPDATE OF a,b SKIP LOCKED LIMIT 100"
    )


async def eligible_signal(conn, signal_id, max_age: int):
    row = await conn.fetchrow(
        "SELECT s.*,m.source_id,m.platform_message_id,m.revision AS message_revision,m.deleted_at,m.media_state,"
        "e.enabled AS expert_enabled,src.enabled AS source_enabled,se.enabled AS trusted,f.phase AS current_phase,"
        "f.sport,f.home_score,f.away_score,q.id AS quote_id,q.price AS current_price,q.source_updated_at,"
        "q.status,q.play_id,q.market_instance_id,q.selection_id,q.market_kind AS current_kind,q.score_basis AS current_basis "
        "FROM v2_expert_signals s JOIN v2_expert_messages m ON m.id=s.message_id "
        "JOIN v2_experts e ON e.id=s.expert_id JOIN v2_expert_sources src ON src.id=m.source_id "
        "JOIN v2_source_experts se ON se.source_id=src.id AND se.expert_id=e.id JOIN v2_fixtures f ON f.id=s.fixture_id "
        "JOIN v2_current_quotes q ON q.fixture_id=s.fixture_id AND q.market_id=s.market_id AND q.period=s.period "
        "AND q.market_kind=s.market_kind AND (q.line IS NOT DISTINCT FROM s.line OR "
        "s.market_kind='winner' AND COALESCE(q.line,0)=0 AND COALESCE(s.line,0)=0) "
        "AND q.selection=s.selection WHERE s.id=$1", signal_id,
    )
    if not row:
        raise HTTPException(409, "推荐及原生盘口不可用")
    now = datetime.now(timezone.utc)
    quote = {"source_updated_at": row["source_updated_at"], "price": row["current_price"], "status": row["status"]}
    if (row["outcome"] != "pending" or row["current_phase"] != "live" or row["sport"] != "football"
            or row["deleted_at"] or row["withdrawn_at"] or row["media_state"] != "ready" or not all((row["expert_enabled"], row["source_enabled"], row["trusted"]))
            or not fresh_quote(quote, max_age, now) or row["recommendation_price"] != row["current_price"]
            or any(not row[key] for key in ("play_id", "market_instance_id", "selection_id"))
            or row["current_kind"] not in {"winner", "handicap", "total"}
            or row["created_at"] < now - timedelta(minutes=5)):
        raise HTTPException(409, "推荐、来源、滚球状态或当前报价不满足实盘条件")
    latest = await conn.fetchval("SELECT max(revision) FROM v2_expert_messages WHERE source_id=$1 AND platform_message_id=$2",
                                 row["source_id"], row["platform_message_id"])
    if latest != row["message_revision"]:
        raise HTTPException(409, "推荐原文已经修订")
    return row


async def account_policy(conn, user_id, settings):
    account = await conn.fetchrow("SELECT a.*,u.disabled_at,u.role FROM v2_leyu_accounts a JOIN v2_users u ON u.id=a.user_id WHERE a.user_id=$1 FOR UPDATE OF a", user_id)
    policy = await conn.fetchrow("SELECT * FROM v2_user_bet_settings WHERE user_id=$1 FOR UPDATE", user_id)
    if (not account or account["disabled_at"] or account['role']!='user' or account["health_state"] != "healthy" or not account["encrypted_session"]
            or account['renewal_state']!='ready' or not account['last_verified_at']
            or (datetime.now(timezone.utc)-account['last_verified_at']).total_seconds()>120
            or not policy or not policy["auto_enabled"] or policy["emergency_stopped"]
            or policy["consent_account_id"] != account["id"] or policy["consent_binding_revision"] != account["binding_revision"]):
        raise HTTPException(409, "账户或本人自动投注许可已失效")
    if not live_enabled(settings) or not await global_enabled(conn, settings):
        raise HTTPException(403, "实际交易尚未被开放")
    if not await has_entitlement(user_id, "automatic_betting", connection=conn):
        raise HTTPException(403, "自动投注会员权益已失效")
    return account, policy


async def exposure_totals(conn, user_id, daily_timezone: str, at: datetime | None = None):
    """Sum the selected local calendar day and every still-reserved order."""
    moment = at if at is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("daily limit reference time requires a timezone")
    return await conn.fetchrow(
        "WITH bounds AS (SELECT date_trunc('day',$3::timestamptz AT TIME ZONE $2::text) AS local_start) "
        "SELECT COALESCE((SELECT sum(stake) FROM v2_bet_orders WHERE user_id=$1 "
        "AND created_at>=b.local_start AT TIME ZONE $2::text "
        "AND created_at<(b.local_start+interval '1 day') AT TIME ZONE $2::text "
        "AND state NOT IN('rejected','cancelled')),0) AS daily,"
        "COALESCE((SELECT sum(reserved_stake) FROM v2_bet_orders WHERE user_id=$1 AND reserved_stake>0),0) AS exposure,"
        "b.local_start AT TIME ZONE $2::text AS day_starts_at,"
        "(b.local_start+interval '1 day') AT TIME ZONE $2::text AS day_ends_at FROM bounds b",
        user_id, daily_timezone, moment,
    )


async def reserve_signal(conn, user_id, signal_id, settings):
    account, policy = await account_policy(conn, user_id, settings)
    previous = await conn.fetchrow("SELECT * FROM v2_bet_orders WHERE user_id=$1 AND signal_id=$2 AND account_id IS NOT NULL", user_id, signal_id)
    if previous:
        return previous
    signal = await eligible_signal(conn, signal_id, await runtime_quote_max_age(settings, conn))
    if signal["expert_id"] not in policy["allowed_experts"]:
        raise HTTPException(403, "本人未允许此专家自动投注")
    stake = policy["stake_per_order"]
    if stake <= 0 or stake > policy["max_per_order"]:
        raise HTTPException(409, "单笔限额不足")
    sums = await exposure_totals(conn, user_id, policy["daily_timezone"])
    if sums["daily"] + stake > policy["daily_limit"] or sums["exposure"] + stake > policy["open_exposure_limit"]:
        raise HTTPException(409, "当日或开放敞口限额不足")
    baseline = signal["score_baseline"] or {}
    score = ""
    if baseline.get("home_score") is not None and baseline.get("away_score") is not None:
        score = f"{baseline['home_score']}:{baseline['away_score']}"
    snapshot = {"match_id": signal["fixture_id"].split(":", 1)[-1], "market_instance_id": signal["market_instance_id"],
                "play_id": signal["play_id"], "selection_id": signal["selection_id"],
                "selection_code": {"home": "1", "away": "2", "draw": "X", "over": "Over", "under": "Under"}.get(signal["selection"]),
                "sport_id": "1", "chpid": signal["market_id"], "kind": signal["current_kind"], "period": signal["period"],
                "line": str(signal["line"]) if signal["line"] is not None else None, "price": str(signal["current_price"]), "phase": "live", "score_benchmark": score}
    identity = uuid.uuid4()
    row = await conn.fetchrow(
        "INSERT INTO v2_bet_orders(id,user_id,signal_id,idempotency_key,state,stake,price,account_id,binding_revision,config_revision,"
        "policy_revision,provider_identity_hash,reserved_stake,quote_snapshot) VALUES($1,$2,$3,$4,'pending',$5,$6,$7,$8,$9,$10,$11,$5,$12) RETURNING *",
        identity, user_id, signal_id, f"expert:{user_id}:{signal_id}", stake, signal["current_price"], account["id"],
        account["binding_revision"], account["config_revision"], policy["revision"], account["provider_identity_hash"], snapshot,
    )
    await audit(conn, user_id, "system", "order_reserved", "bet_order", str(identity), details={"stake": str(stake), "signal_id": str(signal_id)})
    return row


async def cancel_unsent(conn, user_id, reason: str):
    return await conn.execute("UPDATE v2_bet_orders SET state='cancelled',reserved_stake=0,lease_until=NULL,"
                              "result_payload=jsonb_build_object('reason',$2::text),updated_at=now() WHERE user_id=$1 AND state='pending'", user_id, reason)


async def protect_inflight_binding(conn,user_id):
    if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM v2_bet_orders WHERE user_id=$1 AND state='submitting' AND lease_until>now())",user_id):
        raise HTTPException(409,'资金请求仍在途，结束或转入未知核对后才能修改账户绑定')
    await conn.execute("UPDATE v2_bet_orders SET state='unknown',result_payload=jsonb_build_object('reason','binding_after_expired_send'),updated_at=now() "
                       "WHERE user_id=$1 AND state='submitting' AND (lease_until IS NULL OR lease_until<=now())",user_id)


async def recheck_order(conn, order, settings, *, include_signal=False):
    account, policy = await account_policy(conn, order["user_id"], settings)
    if (account["id"] != order["account_id"] or account["binding_revision"] != order["binding_revision"]
            or account["config_revision"] != order["config_revision"] or policy["revision"] != order["policy_revision"]):
        raise HTTPException(409, "账户、会话或本人限额版本已变化")
    signal = await eligible_signal(conn, order["signal_id"], await runtime_quote_max_age(settings, conn))
    if signal["expert_id"] not in policy["allowed_experts"] or order["stake"] > policy["max_per_order"]:
        raise HTTPException(409, "推荐或单笔许可已失效")
    current = order["quote_snapshot"]
    if any(str(signal[key]) != str(current[key]) for key in ("play_id", "market_instance_id", "selection_id")):
        raise HTTPException(409, "原生盘口标识已经变化")
    return (account, signal) if include_signal else account


def prepared_line(value, kind):
    if kind=='winner' and value in (None,''):
        return Decimal('0')
    try:
        parts=str(value).split('/')
        if len(parts) not in (1,2):
            return None
        values=[Decimal(part.strip()) for part in parts]
        if len(values)==2 and parts[0].strip().startswith('-') and not parts[1].strip().startswith(('-','+')):
            values[1]=-abs(values[1])
        return sum(values)/len(values) if all(value.is_finite() for value in values) else None
    except Exception:
        return None


def prepared_line_matches(raw, expected, kind):
    """No-line winner is compatible with legacy zero, never a missing Asian line."""
    if isinstance(raw, bool) or isinstance(expected, bool):
        return False
    actual = prepared_line(raw, kind)
    wanted = prepared_line(expected, kind)
    if kind == 'winner':
        return expected != '' and actual == Decimal('0') and wanted == Decimal('0')
    return actual is not None and wanted is not None and actual == wanted
