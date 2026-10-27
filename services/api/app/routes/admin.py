from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hmac
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from .. import database
from ..auth import audit, require_admin, verify_sensitive_admin_session
from ..security import encrypt_secret, decrypt_secret
from ..membership import has_entitlement
from .operations import collector_health
from .renewal import LEYU_CONFIG_KEYS, LEYU_SECRET_KEYS, revision_locked
from ..runtime_settings import RUNTIME_KEYS, validate_runtime_value

router = APIRouter(prefix="/api/v1/admin")
SECRET_SETTINGS = set(LEYU_SECRET_KEYS) | {"telegram_api_hash", "telegram_session", "leyu_login_password", "leyu_app_signing_config", "leyu_app_discovery_config", "leyu_login_proxy_password_template"}
ALLOWED_SETTINGS = SECRET_SETTINGS | {
    "leyu_api_base", "leyu_request_id", "telegram_api_id", "telegram_phone", "telegram_source_enabled", "telegram_monitored_chats",
    "hermes_analysis_model", "auto_betting_enabled", "quote_max_age_seconds",
} | set(LEYU_CONFIG_KEYS)


class SourceInput(BaseModel):
    chat_id: str = Field(min_length=1, max_length=160)
    display_name: str = Field(min_length=1, max_length=160)
    enabled: bool = True
    allow_media: bool = True

    @field_validator("chat_id")
    @classmethod
    def stable_group_id(cls, value):
        value = value.strip()
        if not value.startswith("-") or not value[1:].isascii() or not value[1:].isdigit():
            raise ValueError("监控来源须使用负数稳定群组编号，请先解析用户名后选择实际群组")
        number = int(value)
        if not -(2 ** 63) <= number < 0:
            raise ValueError("群组编号超出支持范围")
        return str(number)

    @field_validator("display_name")
    @classmethod
    def meaningful_name(cls, value):
        value = value.strip()
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("显示名称不能为空或包含控制字符")
        return value


class SourceExpertInput(BaseModel):
    sender_id: str = Field(min_length=1, max_length=160)
    nickname: str = Field(min_length=1, max_length=120)
    verification_note: str = Field(default="", max_length=500)

    @field_validator("sender_id")
    @classmethod
    def stable_sender_id(cls, value):
        value = value.strip()
        digits = value[1:] if value.startswith("-") else value
        if not digits.isascii() or not digits.isdigit() or not -(2 ** 63) <= int(value) < 2 ** 63 or int(value) == 0:
            raise ValueError("作者须使用非零稳定数字编号，昵称和用户名不能作为身份键")
        return str(int(value))

    @field_validator("nickname", "verification_note")
    @classmethod
    def meaningful_text(cls, value, info):
        value = value.strip()
        if (info.field_name == "nickname" and not value) or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("昵称不能为空，核验说明和昵称不能包含控制字符")
        return value


def configured_sender_identity(sender_id):
    value = str(sender_id)
    kind = "chat_sender" if value.startswith("-") else "user_account" if value.isascii() and value.isdigit() else "unresolved"
    # A posting peer may represent a channel or several anonymous admins. A
    # configured mapping never claims to have identified the person behind it.
    return {"sender_identity_kind": kind, "individual_author_resolved": False}


def _safe_setting_value(key: str, value: str | None):
    """Return an audit-safe representation of a setting value.

    Secret settings must never be copied into an audit record, even when the
    caller changes only a non-secret setting in the same request.  Plain
    settings are bounded so an accidentally large value cannot make the audit
    table a second configuration store.
    """
    if key in SECRET_SETTINGS:
        return {"configured": bool(value), "value": None}
    if value is None:
        return {"configured": False, "value": None}
    text = str(value)
    return {"configured": True, "value": text[:256], "truncated": len(text) > 256}


def _setting_diff(key: str, before: str | None, after: str | None) -> dict:
    if key in SECRET_SETTINGS:
        before_configured = bool(before)
        after_configured = bool(after)
        # Comparing secret material is done in memory only.  The result is a
        # boolean and the value itself is never serialized to JSON/audit logs.
        equal = (before is None and after is None) or (
            before is not None and after is not None and hmac.compare_digest(str(before), str(after))
        )
        return {
            "before": _safe_setting_value(key, before),
            "after": _safe_setting_value(key, after),
            "changed": not (before_configured == after_configured == True and equal),
        }
    return {
        "before": _safe_setting_value(key, before),
        "after": _safe_setting_value(key, after),
        "changed": before != after,
    }


class SettingsInput(BaseModel):
    values: dict[str, str | int | bool] = Field(max_length=30)
    expected_leyu_version: int | None = Field(default=None, ge=1)
    expected_model_revision: int | None = Field(default=None, ge=1)


class ModelCandidateInput(BaseModel):
    model: str = Field(max_length=200)
    provider: str | None = Field(default=None, max_length=200)
    provenance: dict[str, str]
    expected_revision: int = Field(ge=1)


class ModelEvidenceInput(BaseModel):
    expected_revision: int = Field(ge=1)
    report_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    report: dict


class ModelReviewDecision(BaseModel):
    sample_id: str = Field(min_length=1, max_length=200)
    decision: str = Field(pattern='^(accept|reject)$')
    note: str = Field(min_length=3, max_length=500)


class ModelReviewInput(BaseModel):
    expected_revision: int = Field(ge=1)
    report_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    reason: str = Field(min_length=3, max_length=500)
    decisions: list[ModelReviewDecision] = Field(max_length=1000)


class ModelActivationInput(BaseModel):
    expected_revision: int = Field(ge=1)


class PlanInput(BaseModel):
    code: str = Field(pattern="^[a-z0-9_-]{2,32}$")
    name: str = Field(min_length=1, max_length=80)
    price: Decimal = Field(ge=0, le=1000000, max_digits=12, decimal_places=2)
    currency: str = Field(default="CNY", pattern="^[A-Z]{3}$")
    duration_days: int = Field(gt=0, le=3650)
    entitlements: dict[str, bool] = Field(default_factory=dict, max_length=30)
    enabled: bool = True


class GrantInput(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    plan_code: str = Field(min_length=2, max_length=32)
    days: int = Field(gt=0, le=3650)
    idempotency_key: uuid.UUID
    reason: str = Field(min_length=3, max_length=500)


class RevokeInput(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@router.get("/status")
async def status(request: Request):
    identity = await require_admin(request)
    pool = database.current_pool()
    db_ok = await pool.fetchval("SELECT 1") == 1
    row = await pool.fetchrow("SELECT count(*) AS queued FROM (SELECT 1 FROM v2_analysis_jobs WHERE state IN ('queued','retry') LIMIT 5001) bounded")
    sources = await pool.fetchval("SELECT count(*) FROM v2_expert_sources WHERE enabled")
    revision_row = await pool.fetchrow(
        "SELECT revision,updated_at FROM v2_configuration_revisions WHERE namespace='leyu'"
    )
    return {
        "database": "healthy" if db_ok else "unavailable",
        "hermes": await request.app.state.hermes_probe.check(),
        "hermes_provider": "unverified",
        "telegram_sources": sources,
        "review_queue": min(row["queued"], 5000),
        "review_queue_capped": row["queued"] > 5000,
        "actor": identity["email"],
        # The API persists a version atomically; the collector must explicitly
        # acknowledge consuming it before a UI can claim the hot update was
        # applied.  No acknowledgement channel exists here, so report that
        # limitation instead of presenting persistence as runtime health.
        "leyu_configuration": {
            "revision": revision_row["revision"] if revision_row else None,
            "updated_at": revision_row["updated_at"] if revision_row else None,
            "application_state": "persisted_unverified",
            "runtime_acknowledged": False,
            "reason": "配置已持久化，等待采集服务回执",
        },
    }


@router.get("/sources")
async def list_sources(request: Request):
    await require_admin(request)
    rows = await database.current_pool().fetch(
        "SELECT s.id,s.platform_chat_id AS chat_id,s.display_name,s.enabled,s.allow_media,s.updated_at,"
        "COUNT(se.expert_id) FILTER (WHERE se.enabled) AS trusted_admins "
        "FROM v2_expert_sources s LEFT JOIN v2_source_experts se ON se.source_id=s.id "
        "GROUP BY s.id ORDER BY s.display_name"
    )
    return {"items": [dict(row) for row in rows]}


@router.post("/sources", status_code=201)
async def create_source(body: SourceInput, request: Request):
    admin = await require_admin(request)
    pool = database.current_pool()
    source_id = uuid.uuid4()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await verify_sensitive_admin_session(conn, admin)
            try:
                await conn.execute(
                    "INSERT INTO v2_expert_sources(id,platform_chat_id,display_name,enabled,allow_media) VALUES($1,$2,$3,$4,$5)",
                    source_id, body.chat_id.strip(), body.display_name.strip(), body.enabled, body.allow_media,
                )
            except Exception as exc:
                if getattr(exc, "sqlstate", None) == "23505":
                    raise HTTPException(status_code=409, detail="该 Telegram 来源已配置") from exc
                raise
            await audit(conn, admin["id"], "admin", "telegram_source_created", "telegram_source", str(source_id))
    return {"id": str(source_id), "chat_id": body.chat_id, "display_name": body.display_name,
            "enabled": body.enabled, "allow_media": body.allow_media}


@router.put("/sources/{source_id}")
async def update_source(source_id: uuid.UUID, body: SourceInput, request: Request):
    admin = await require_admin(request)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            await verify_sensitive_admin_session(conn, admin)
            existing = await conn.fetchval("SELECT platform_chat_id FROM v2_expert_sources WHERE id=$1 FOR UPDATE", source_id)
            if existing is not None and existing != body.chat_id:
                raise HTTPException(409, "来源群组编号不可替换，请新建来源以保留原群组证据与信任映射")
            row = await conn.execute(
                "UPDATE v2_expert_sources SET platform_chat_id=$2,display_name=$3,enabled=$4,allow_media=$5,updated_at=now() WHERE id=$1",
                source_id, body.chat_id.strip(), body.display_name.strip(), body.enabled, body.allow_media,
            )
            if row.endswith(" 0"):
                raise HTTPException(status_code=404, detail="来源不存在")
            await audit(conn, admin["id"], "admin", "telegram_source_updated", "telegram_source", str(source_id))
    return {"id": str(source_id), "updated": True}


@router.get("/sources/{source_id}/experts")
async def list_source_experts(source_id: uuid.UUID, request: Request):
    await require_admin(request)
    rows = await database.current_pool().fetch(
        "SELECT e.id,e.platform_sender_id,e.display_nickname,se.enabled,se.added_at "
        "FROM v2_source_experts se JOIN v2_experts e ON e.id=se.expert_id "
        "WHERE se.source_id=$1 ORDER BY e.display_nickname", source_id,
    )
    return {"items": [dict(row, source_id=source_id, trust_basis="configured_mapping", telegram_admin_verified=False,
                            **configured_sender_identity(row["platform_sender_id"])) for row in rows]}


@router.get('/sources/{source_id}/directory')
async def source_directory(source_id: uuid.UUID, request: Request):
    await require_admin(request)
    pool = database.current_pool()
    if not await pool.fetchval('SELECT 1 FROM v2_expert_sources WHERE id=$1', source_id):
        raise HTTPException(404, '来源不存在')
    row = await pool.fetchrow('SELECT state,reason,retry_after,admins,last_attempt_at,last_success_at FROM v2_telegram_directories WHERE source_id=$1', source_id)
    if not row:
        return {'state': 'pending', 'items': [], 'automatic': True}
    return {key: value for key, value in dict(row).items() if key!='admins'} | {
        'items': row['admins'], 'automatic': True, 'trust_automatically_granted': False}


@router.post("/sources/{source_id}/experts", status_code=201)
async def add_source_expert(source_id: uuid.UUID, body: SourceExpertInput, request: Request):
    admin = await require_admin(request)
    pool = database.current_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await verify_sensitive_admin_session(conn, admin)
            if not await conn.fetchval("SELECT 1 FROM v2_expert_sources WHERE id=$1", source_id):
                raise HTTPException(status_code=404, detail="来源不存在")
            expert_id = await conn.fetchval(
                "INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,$2,$3) "
                "ON CONFLICT(platform_sender_id) DO UPDATE SET display_nickname=EXCLUDED.display_nickname,updated_at=now() "
                "RETURNING id", uuid.uuid4(), body.sender_id.strip(), body.nickname.strip(),
            )
            await conn.execute(
                "INSERT INTO v2_source_experts(source_id,expert_id,enabled) VALUES($1,$2,TRUE) "
                "ON CONFLICT(source_id,expert_id) DO UPDATE SET enabled=TRUE", source_id, expert_id,
            )
            await audit(conn, admin["id"], "admin", "telegram_expert_trusted", "expert", str(expert_id), details={
                "source_id": str(source_id), "sender_id": body.sender_id, "trust_basis": "manual", "telegram_admin_verified": False,
                "verification_note": body.verification_note, **configured_sender_identity(body.sender_id),
            })
    return {"id": str(expert_id), "source_id": str(source_id), "sender_id": body.sender_id, "nickname": body.nickname, "enabled": True,
            "trust_basis": "manual", "telegram_admin_verified": False, **configured_sender_identity(body.sender_id)}


@router.delete("/sources/{source_id}/experts/{expert_id}")
async def remove_source_expert(source_id: uuid.UUID, expert_id: uuid.UUID, request: Request):
    admin = await require_admin(request)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            await verify_sensitive_admin_session(conn, admin)
            result = await conn.execute(
                "UPDATE v2_source_experts SET enabled=FALSE WHERE source_id=$1 AND expert_id=$2", source_id, expert_id,
            )
            if result.endswith(" 0"):
                raise HTTPException(status_code=404, detail="来源管理员未配置")
            await audit(conn, admin["id"], "admin", "telegram_expert_untrusted", "expert", str(expert_id), details={"source_id": str(source_id)})
    return {"disabled": True}


@router.get("/settings")
async def get_settings(request: Request):
    await require_admin(request)
    rows = await database.current_pool().fetch("SELECT setting_key,encrypted_value,updated_at FROM v2_admin_settings")
    settings = request.app.state.settings
    output = {
        key: {"configured": False, "masked": key in SECRET_SETTINGS,
              "value": None if key in SECRET_SETTINGS else ""}
        for key in ALLOWED_SETTINGS
    }
    for row in rows:
        key = row["setting_key"]
        if key not in ALLOWED_SETTINGS:
            continue
        try:
            value = decrypt_secret(settings.encryption_key, row["encrypted_value"])
        except Exception:
            output[key] = {"configured": True, "masked": True, "value": None,
                           "state": "unreadable", "reason": "configuration_key_or_ciphertext_invalid"}
            continue
        output[key] = {
            "configured": True,
            "masked": key in SECRET_SETTINGS,
            "value": None if key in SECRET_SETTINGS else value,
            "updated_at": row["updated_at"],
        }
    version = await database.current_pool().fetchval("SELECT revision FROM v2_configuration_revisions WHERE namespace='leyu'")
    collector = await collector_health(request)
    leyu_health = collector.get("sources", {}).get("leyu", {}) if collector.get("state") == "reachable" else {}
    applied_version = leyu_health.get("config_version")
    runtime_state = (
        "invalid" if leyu_health.get("config_validation_error") else
        "applied" if applied_version == version else
        "pending" if collector.get("state") == "reachable" else
        "unavailable"
    )
    from ..model_configuration import read_state, public_state
    model_state = await read_state(database.current_pool(), settings)
    output['hermes_analysis_model'] = {**output['hermes_analysis_model'], 'value': model_state['active']['model'] or '',
        'state': 'active', 'pending_candidates': sum(item['status'] != 'activated' for item in model_state['candidates'])}
    return {
        "items": output,
        "allowed_keys": sorted(ALLOWED_SETTINGS),
        "leyu_version": version,
        'model_configuration': public_state(model_state),
        "runtime": {
            "state": runtime_state,
            "desired_version": version,
            "applied_version": applied_version,
            "validated_at": leyu_health.get("config_validated_at"),
            "source_state": leyu_health.get("state"),
            "reason": leyu_health.get("last_error") or leyu_health.get("config_validation_error") or collector.get("reason"),
        },
    }


@router.put("/settings")
async def update_settings(body: SettingsInput, request: Request):
    admin = await require_admin(request)
    unknown = set(body.values) - ALLOWED_SETTINGS
    if unknown:
        raise HTTPException(status_code=422, detail="包含不允许的配置键")
    for key in set(body.values) & RUNTIME_KEYS:
        try:
            body.values[key] = validate_runtime_value(key, body.values[key])
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from None
    relay = str(body.values.get("leyu_collection_relay", "")).strip()
    if relay:
        try:
            parts = urlsplit(relay)
            port = parts.port
        except ValueError:
            raise HTTPException(status_code=422, detail="采集中转地址无效") from None
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.path not in {"", "/"} or parts.query or parts.fragment
                or (port is not None and not 1 <= port <= 65535)
                or "\\" in relay
                or any(ord(c) <= 32 or ord(c) == 127 for c in relay)):
            raise HTTPException(status_code=422, detail="采集中转必须为无路径、无凭据的 HTTPS 来源")
    pool = database.current_pool()
    model_candidate = None
    async with pool.acquire() as conn:
        async with conn.transaction():
            if 'hermes_analysis_model' in body.values:
                from ..model_configuration import lock
                await lock(conn)
            await verify_sensitive_admin_session(conn, admin)
            leyu_keys = set(body.values) & LEYU_CONFIG_KEYS
            version = None
            if leyu_keys:
                version = await revision_locked(conn)
                if version != body.expected_leyu_version:
                    raise HTTPException(status_code=409, detail="乐鱼配置版本已变化，请刷新后重试")
            changed = []
            before_values: dict[str, str | None] = {}
            for key, value in body.values.items():
                text = str(value).strip()
                if key == 'hermes_analysis_model':
                    from ..model_configuration import stage
                    if body.expected_model_revision is None:
                        raise HTTPException(409, '模型候选需要当前配置版本，请刷新设置')
                    model_candidate, _ = await stage(conn, request.app.state.settings, admin, text,
                        expected_revision=body.expected_model_revision, request_id=getattr(request.state, 'request_id', ''))
                    continue
                if not text and key in SECRET_SETTINGS:
                    continue
                if len(text) > 4096:
                    raise HTTPException(status_code=422, detail="配置值超出长度限制")
                previous_ciphertext = await conn.fetchval(
                    "SELECT encrypted_value FROM v2_admin_settings WHERE setting_key=$1 FOR UPDATE", key
                )
                previous = None
                if previous_ciphertext is not None:
                    try:
                        previous = decrypt_secret(request.app.state.settings.encryption_key, previous_ciphertext)
                    except Exception:
                        # Do not copy an undecryptable ciphertext into an
                        # audit record.  A replacement remains allowed and is
                        # represented as an unconfigured prior value.
                        previous = None
                before_values[key] = previous
                encrypted = encrypt_secret(request.app.state.settings.encryption_key, text)
                await conn.execute(
                    "INSERT INTO v2_admin_settings(setting_key,encrypted_value,updated_by,updated_at) VALUES($1,$2,$3,now()) "
                    "ON CONFLICT(setting_key) DO UPDATE SET encrypted_value=EXCLUDED.encrypted_value,updated_by=EXCLUDED.updated_by,updated_at=now()",
                    key, encrypted, admin["id"],
                )
                changed.append(key)
            if leyu_keys & set(changed):
                version = await conn.fetchval("UPDATE v2_configuration_revisions SET revision=revision+1,updated_at=now() WHERE namespace='leyu' RETURNING revision")
            details = {
                "changed_keys": sorted(changed),
                "leyu_version": version,
                "configuration_version": version,
                "request_id": str(getattr(request.state, "request_id", ""))[:128],
                "diff": {
                    key: _setting_diff(key, before_values.get(key), str(body.values[key]).strip())
                    for key in changed
                },
                "application_state": "persisted_unverified" if leyu_keys & set(changed) else "persisted",
                "runtime_acknowledged": False,
            }
            await audit(conn, admin["id"], "admin", "settings_updated", "admin_settings", "",
                        getattr(request.state, "request_id", ""), details=details)
    application = {
        "state": "model_candidate_pending" if model_candidate else "persisted_unverified" if leyu_keys & set(changed) else "persisted",
        "requested_revision": version,
        "runtime_acknowledged": False,
        "reason": "新模型仅保存为待验证候选，当前模型继续生效；需比较证据、人工复核与显式激活" if model_candidate else "配置已写入数据库，等待采集服务回执" if leyu_keys & set(changed) else "配置已写入数据库",
    }
    return {"updated": sorted(changed), "secrets_masked": sorted(SECRET_SETTINGS & set(body.values)),
            "leyu_version": version, "application": application, 'model_candidate': model_candidate}


@router.get('/model-configuration')
async def model_configuration_status(request: Request):
    await require_admin(request)
    from ..model_configuration import public_state, read_state
    return public_state(await read_state(database.current_pool(), request.app.state.settings))


@router.post('/model-configuration/candidates', status_code=201)
async def create_model_candidate(body: ModelCandidateInput, request: Request):
    identity = await require_admin(request)
    from ..model_configuration import public_state, stage
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            candidate, state = await stage(conn, request.app.state.settings, identity, body.model,
                provider=body.provider, provenance=body.provenance, expected_revision=body.expected_revision,
                request_id=getattr(request.state, 'request_id', ''))
    return {'candidate': candidate, 'configuration': public_state(state)}


async def _model_candidate_operation(request, candidate_id, body, operation):
    identity = await require_admin(request)
    from ..model_configuration import mutate
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            return await mutate(conn, request.app.state.settings, identity, candidate_id, body.expected_revision,
                operation, body.model_dump(mode='json'), getattr(request.state, 'request_id', ''))


@router.post('/model-configuration/candidates/{candidate_id}/evidence')
async def attach_model_evidence(candidate_id: uuid.UUID, body: ModelEvidenceInput, request: Request):
    return await _model_candidate_operation(request, candidate_id, body, 'evidence')


@router.post('/model-configuration/candidates/{candidate_id}/review')
async def review_model_candidate(candidate_id: uuid.UUID, body: ModelReviewInput, request: Request):
    return await _model_candidate_operation(request, candidate_id, body, 'review')


@router.post('/model-configuration/candidates/{candidate_id}/activate')
async def activate_model_candidate(candidate_id: uuid.UUID, body: ModelActivationInput, request: Request):
    return await _model_candidate_operation(request, candidate_id, body, 'activate')


@router.get("/membership/plans")
async def list_plans(request: Request):
    await require_admin(request)
    rows = await database.current_pool().fetch(
        "SELECT id,code,name,price,currency,duration_days,entitlements,enabled,updated_at "
        "FROM v2_membership_plans ORDER BY price,duration_days"
    )
    return {"items": [dict(row) for row in rows]}


@router.put("/membership/plans/{code}")
async def upsert_plan(code: str, body: PlanInput, request: Request):
    admin = await require_admin(request)
    if code != body.code:
        raise HTTPException(status_code=422, detail="套餐代码与路径不一致")
    plan_id = uuid.uuid4()
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            await verify_sensitive_admin_session(conn, admin)
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"membership-plan:{code}")
            previous = await conn.fetchrow(
                "SELECT name,price,currency,duration_days,entitlements,enabled FROM v2_membership_plans WHERE code=$1 FOR UPDATE", code,
            )
            plan_id = await conn.fetchval(
                "INSERT INTO v2_membership_plans(id,code,name,price,currency,duration_days,entitlements,enabled) "
                "VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT(code) DO UPDATE SET name=EXCLUDED.name,"
                "price=EXCLUDED.price,currency=EXCLUDED.currency,duration_days=EXCLUDED.duration_days,"
                "entitlements=EXCLUDED.entitlements,enabled=EXCLUDED.enabled,updated_at=now() RETURNING id",
                plan_id, body.code, body.name, body.price, body.currency, body.duration_days,
                body.entitlements, body.enabled,
            )
            after = body.model_dump(mode="json", exclude={"code"})
            before = dict(previous) if previous else None
            if before is not None:
                before["price"] = str(before["price"])
            await audit(conn, admin["id"], "admin", "membership_plan_updated", "membership_plan", body.code,
                        getattr(request.state, "request_id", ""), {"before": before, "after": after})
    return {"id": str(plan_id), "code": body.code, "enabled": body.enabled}


@router.post("/membership/grants", status_code=201)
async def grant_membership(body: GrantInput, request: Request):
    admin = await require_admin(request)
    pool = database.current_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await verify_sensitive_admin_session(conn, admin)
            reason = body.reason.strip()
            if len(reason) < 3:
                raise HTTPException(status_code=422, detail="开通原因至少三个非空字符")
            reference = f"admin:{admin['id']}:{body.idempotency_key}"
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", reference)
            existing = await conn.fetchrow(
                "SELECT m.*,u.email,p.code FROM v2_memberships m JOIN v2_users u ON u.id=m.user_id "
                "JOIN v2_membership_plans p ON p.id=m.plan_id WHERE m.grant_source='admin' AND m.external_reference=$1",
                reference,
            )
            if existing:
                if (existing["email"] != body.email.strip().lower() or existing["code"] != body.plan_code
                        or existing["ends_at"] - existing["starts_at"] != timedelta(days=body.days)
                        or existing["grant_reason"] != reason):
                    raise HTTPException(status_code=409, detail="幂等标识已被不同开通请求使用")
                return {"id": str(existing["id"]), "user_id": str(existing["user_id"]), "plan": body.plan_code,
                        "days": body.days, "starts_at": existing["starts_at"], "ends_at": existing["ends_at"], "replayed": True}
            user = await conn.fetchrow("SELECT id FROM v2_users WHERE email=$1 AND role='user' AND disabled_at IS NULL FOR UPDATE", body.email.strip().lower())
            plan = await conn.fetchrow("SELECT id,code FROM v2_membership_plans WHERE code=$1 AND enabled", body.plan_code)
            if not user or not plan:
                raise HTTPException(status_code=404, detail="有效用户或套餐不存在")
            previous = await conn.fetchrow(
                "SELECT ends_at FROM v2_memberships WHERE user_id=$1 AND plan_id=$2 AND status='active' "
                "AND (ends_at IS NULL OR ends_at>now()) ORDER BY ends_at DESC NULLS FIRST LIMIT 1",
                user["id"], plan["id"],
            )
            if previous and previous["ends_at"] is None:
                raise HTTPException(status_code=409, detail="此套餐已有永久权益，无需续期")
            starts_at = max(datetime.now(timezone.utc), previous["ends_at"]) if previous else datetime.now(timezone.utc)
            ends_at = starts_at + timedelta(days=body.days)
            membership_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO v2_memberships(id,user_id,plan_id,status,starts_at,ends_at,grant_source,external_reference,grant_reason) "
                "VALUES($1,$2,$3,'active',$4,$5,'admin',$6,$7)",
                membership_id, user["id"], plan["id"], starts_at, ends_at, reference, reason,
            )
            await audit(
                conn, admin["id"], "admin", "membership_granted", "membership", str(membership_id),
                getattr(request.state, "request_id", ""), {"reason": reason, "days": body.days, "plan": body.plan_code},
            )
    return {"id": str(membership_id), "user_id": str(user["id"]), "plan": body.plan_code, "days": body.days,
            "starts_at": starts_at, "ends_at": ends_at, "replayed": False}


@router.get("/users")
async def list_users(request: Request, limit: int = 50, after: uuid.UUID | None = None,
                     q: str = Query(default='', max_length=120)):
    await require_admin(request)
    rows = await database.current_pool().fetch(
        "SELECT id,email,display_name,role,disabled_at,created_at FROM v2_users "
        "WHERE ($1::uuid IS NULL OR id>$1) AND ($3='' OR email ILIKE '%'||$3||'%' OR display_name ILIKE '%'||$3||'%') "
        "ORDER BY id LIMIT $2", after, max(1, min(limit, 100)) + 1, q.strip(),
    )
    page = rows[:max(1, min(limit, 100))]
    return {"items": [dict(row) for row in page], "next_cursor": str(page[-1]["id"]) if len(rows) > len(page) else None}


@router.get("/users/{user_id}/memberships")
async def list_memberships(user_id: uuid.UUID, request: Request):
    await require_admin(request)
    rows = await database.current_pool().fetch(
        "SELECT m.id,m.status,m.starts_at,m.ends_at,m.grant_source,m.grant_reason,p.code,p.name,p.enabled "
        "FROM v2_memberships m JOIN v2_membership_plans p ON p.id=m.plan_id WHERE m.user_id=$1 "
        "ORDER BY m.created_at DESC,m.id DESC LIMIT 200", user_id,
    )
    return {"items": [dict(row) for row in rows]}


@router.post("/membership/grants/{membership_id}/revoke")
async def revoke_membership(membership_id: uuid.UUID, body: RevokeInput, request: Request):
    admin = await require_admin(request)
    reason = body.reason.strip()
    if len(reason) < 3:
        raise HTTPException(status_code=422, detail="撤销原因至少三个非空字符")
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            await verify_sensitive_admin_session(conn, admin)
            row = await conn.fetchrow("SELECT user_id,status FROM v2_memberships WHERE id=$1 FOR UPDATE", membership_id)
            if not row:
                raise HTTPException(status_code=404, detail="会员授予不存在")
            if row["status"] != "cancelled":
                await conn.execute("UPDATE v2_memberships SET status='cancelled' WHERE id=$1", membership_id)
                if not await has_entitlement(row["user_id"], "automatic_betting", conn):
                    await conn.execute(
                        "UPDATE v2_user_bet_settings SET auto_enabled=FALSE,emergency_stopped=TRUE,updated_at=now() WHERE user_id=$1",
                        row["user_id"],
                    )
                await audit(conn, admin["id"], "admin", "membership_revoked", "membership", str(membership_id),
                            getattr(request.state, "request_id", ""), {"reason": reason})
    return {"id": str(membership_id), "status": "cancelled"}


@router.get("/audit")
async def audit_events(request: Request, limit: int = 100):
    await require_admin(request)
    limit = max(1, min(limit, 500))
    rows = await database.current_pool().fetch(
        "SELECT id,actor_id,actor_role,action,resource_type,resource_id,request_id,details,created_at "
        "FROM v2_audit_log ORDER BY created_at DESC,id DESC LIMIT $1", limit,
    )
    return {"items": [dict(row) for row in rows]}
