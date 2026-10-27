"""受限乐鱼采集配置与远端续期互斥；不含任何订单执行能力。"""

from __future__ import annotations

from datetime import datetime, timezone
import uuid
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import database
from ..auth import audit, require_admin
from ..security import decrypt_secret, encrypt_secret
from .internal import _authorized

router = APIRouter()
LEYU_SECRET_KEYS = frozenset({
    "leyu_request_id", "leyu_api_token", "leyu_business_cookie_token", "leyu_cookie",
    "leyu_app_token", "leyu_app_signature", "leyu_app_login_signature", "leyu_app_uuid",
    "leyu_app_login_name", "leyu_app_login_password", "leyu_login_proxy_url",
    "leyu_h5_token", "leyu_h5_signature", "leyu_h5_uuid",
})
LEYU_CONFIG_KEYS = LEYU_SECRET_KEYS | frozenset({
    "leyu_api_base", "leyu_origin", "leyu_user_agent", "leyu_cuid", "leyu_check_id_base",
    "leyu_batch_size", "leyu_batch_concurrency", "leyu_batch_interval_seconds", "leyu_app_host",
    "leyu_app_signing_config", "leyu_app_discovery_config",
    "leyu_login_proxy_password_template", "leyu_domain_discovery_url", "leyu_h5_site", "leyu_venue",
})
PERSIST_KEYS = frozenset({"leyu_request_id", "leyu_app_token", "leyu_api_base", "leyu_origin", "leyu_app_host"})


class ClaimInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_hash: str = Field(pattern="^[a-f0-9]{64}$")
    expected_version: int = Field(ge=1)


class AttemptDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str = Field(default="", max_length=80, pattern="^[A-Za-z0-9_]*$")
    code: str = Field(default="", pattern="^[0-9]{0,12}$")
    login_submitted: bool = False


class FinishInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["success", "failed", "unknown", "rejected"]
    metadata: AttemptDetails = Field(default_factory=AttemptDetails)


class PersistInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: uuid.UUID
    expected_version: int = Field(ge=1)
    values: dict[str, str] = Field(min_length=1, max_length=5)


class ResetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=8, max_length=400)


def authorized(request: Request) -> None:
    _authorized(request, request.app.state.settings.collector_token)


async def revision_locked(conn) -> int:
    return await conn.fetchval("SELECT revision FROM v2_configuration_revisions WHERE namespace='leyu' FOR UPDATE")


@router.get("/internal/collector/renewal/config")
async def renewal_config(request: Request):
    authorized(request)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            revision = await conn.fetchval("SELECT revision FROM v2_configuration_revisions WHERE namespace='leyu'")
            rows = await conn.fetch("SELECT setting_key,encrypted_value FROM v2_admin_settings WHERE setting_key=ANY($1::text[])", list(LEYU_CONFIG_KEYS))
            values = {}
            for row in rows:
                try:
                    values[row["setting_key"]] = decrypt_secret(request.app.state.settings.encryption_key, row["encrypted_value"])
                except Exception:
                    raise HTTPException(status_code=503, detail="乐鱼配置解密失败，需核对密钥版本") from None
    return {"version": revision, "items": values}


@router.post("/internal/collector/renewal/claim", status_code=201)
async def claim_renewal(body: ClaimInput, request: Request):
    authorized(request)
    failure = None
    result = None
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            version = await revision_locked(conn)
            if version != body.expected_version:
                raise HTTPException(status_code=409, detail="配置版本已变化，请重载后续期")
            # 此路由服务全局采集身份。换 account_hash 不能绕过同一配置的运行锁、未知状态或冷却。
            other = await conn.fetchrow(
                "SELECT account_hash,state,retry_after FROM v2_renewal_accounts WHERE account_hash<>$1 "
                "AND (state IN ('running','unknown','rejected') OR retry_after>now()) ORDER BY updated_at DESC LIMIT 1 FOR UPDATE",
                body.account_hash,
            )
            if other:
                raise HTTPException(status_code=409, detail="全局采集续期仍在运行、冷却或待核对，不可切换身份绕过")
            await conn.execute("INSERT INTO v2_renewal_accounts(account_hash) VALUES($1) ON CONFLICT DO NOTHING", body.account_hash)
            row = await conn.fetchrow("SELECT * FROM v2_renewal_accounts WHERE account_hash=$1 FOR UPDATE", body.account_hash)
            now = datetime.now(timezone.utc)
            if row["state"] == "running" and row["lease_until"] and row["lease_until"] <= now:
                await conn.execute("UPDATE v2_renewal_accounts SET state='unknown',updated_at=now() WHERE account_hash=$1", body.account_hash)
                await conn.execute("UPDATE v2_renewal_attempts SET state='unknown',finished_at=now(),details=$2 WHERE id=$1 AND state='running'",
                                   row["active_attempt"], {"category": "expired_attempt", "login_submitted": True})
                await audit(conn, None, "collector", "renewal_attempt_expired", "leyu_account", body.account_hash,
                            request_id=getattr(request.state, "request_id", ""))
                failure = "上次续期结果未知，需管理员核对后重新授权"
            elif row["state"] in {"unknown", "rejected"}:
                failure = "上次续期结果未知或被拒绝，需管理员核对后重新授权"
            elif row["state"] == "running":
                failure = "已有续期任务进行中"
            elif row["retry_after"] and row["retry_after"] > now:
                failure = "续期冷却中"
            if not failure:
                attempt = uuid.uuid4()
                await conn.execute("INSERT INTO v2_renewal_attempts(id,account_hash,config_revision) VALUES($1,$2,$3)", attempt, body.account_hash, version)
                await conn.execute("UPDATE v2_renewal_accounts SET state='running',active_attempt=$2,lease_until=now()+interval '180 seconds',updated_at=now() WHERE account_hash=$1",
                                   body.account_hash, attempt)
                await audit(conn, None, "collector", "renewal_claimed", "leyu_account", body.account_hash,
                            request_id=getattr(request.state, "request_id", ""), details={"attempt_id": str(attempt), "config_revision": version})
                result = {"attempt_id": str(attempt), "version": version}
    if failure:
        raise HTTPException(status_code=409, detail=failure)
    return result


@router.post("/internal/collector/renewal/{attempt_id}/finish")
async def finish_renewal(attempt_id: uuid.UUID, body: FinishInput, request: Request):
    authorized(request)
    expired = False
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            attempt = await conn.fetchrow("SELECT account_hash,state FROM v2_renewal_attempts WHERE id=$1", attempt_id)
            if not attempt:
                raise HTTPException(status_code=404, detail="续期任务不存在")
            account = await conn.fetchrow("SELECT * FROM v2_renewal_accounts WHERE account_hash=$1 FOR UPDATE", attempt["account_hash"])
            current = await conn.fetchrow("SELECT state FROM v2_renewal_attempts WHERE id=$1 FOR UPDATE", attempt_id)
            if current["state"] != "running":
                if current["state"] == body.status:
                    return {"status": current["state"], "idempotent": True}
                raise HTTPException(status_code=409, detail="续期终态不可覆盖")
            if account["active_attempt"] != attempt_id:
                raise HTTPException(status_code=409, detail="续期任务已失效")
            details = body.metadata.model_dump()
            expired = not account["lease_until"] or account["lease_until"] <= datetime.now(timezone.utc)
            status = "unknown" if expired else body.status
            if expired:
                details["category"] = "expired_finish"
            await conn.execute("UPDATE v2_renewal_attempts SET state=$2,details=$3,finished_at=now() WHERE id=$1", attempt_id, status, details)
            state = status if status in {"unknown", "rejected"} else "ready"
            cooldown = 90 if body.status == "success" else 30
            await conn.execute("UPDATE v2_renewal_accounts SET state=$2,active_attempt=NULL,lease_until=NULL,retry_after=now()+make_interval(secs=>$3),updated_at=now() WHERE account_hash=$1",
                               attempt["account_hash"], state, cooldown)
            await audit(conn, None, "collector", "renewal_finished", "leyu_account", attempt["account_hash"],
                        request_id=getattr(request.state, "request_id", ""), details={"attempt_id": str(attempt_id), "status": status, **details})
    if expired:
        raise HTTPException(status_code=409, detail="续期租约已过期，结果按未知保存，需管理员核对")
    return {"status": body.status, "idempotent": False}


@router.put("/internal/collector/renewal/config")
async def persist_renewal(body: PersistInput, request: Request):
    authorized(request)
    if set(body.values) - PERSIST_KEYS:
        raise HTTPException(status_code=422, detail="续期不得写入其他配置")
    for key, value in body.values.items():
        if not value or len(value) > 8192 or "\n" in value or "\r" in value:
            raise HTTPException(status_code=422, detail="续期配置值无效")
        if key in {"leyu_api_base", "leyu_origin", "leyu_app_host"}:
            parts = urlsplit(value)
            if parts.scheme != "https" or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment or parts.path not in {"", "/"}:
                raise HTTPException(status_code=422, detail="续期域名必须为 HTTPS 来源")
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            version = await revision_locked(conn)
            if version != body.expected_version:
                raise HTTPException(status_code=409, detail="配置版本冲突，不能覆盖热修改")
            attempt = await conn.fetchrow("SELECT account_hash,state,config_revision FROM v2_renewal_attempts WHERE id=$1", body.attempt_id)
            if not attempt:
                raise HTTPException(status_code=404, detail="续期任务不存在")
            account = await conn.fetchrow("SELECT active_attempt,state,lease_until FROM v2_renewal_accounts WHERE account_hash=$1 FOR UPDATE", attempt["account_hash"])
            if (attempt["state"] != "running" or account["state"] != "running" or account["active_attempt"] != body.attempt_id
                    or not account["lease_until"] or account["lease_until"] <= datetime.now(timezone.utc)
                    or attempt["config_revision"] != version):
                raise HTTPException(status_code=409, detail="续期租约已失效")
            for key, value in body.values.items():
                encrypted = encrypt_secret(request.app.state.settings.encryption_key, value)
                await conn.execute("INSERT INTO v2_admin_settings(setting_key,encrypted_value,updated_at) VALUES($1,$2,now()) ON CONFLICT(setting_key) DO UPDATE SET encrypted_value=EXCLUDED.encrypted_value,updated_by=NULL,updated_at=now()", key, encrypted)
            version = await conn.fetchval("UPDATE v2_configuration_revisions SET revision=revision+1,updated_at=now() WHERE namespace='leyu' RETURNING revision")
            await conn.execute("UPDATE v2_renewal_attempts SET config_revision=$2 WHERE id=$1", body.attempt_id, version)
            await audit(conn, None, "collector", "renewal_config_updated", "leyu_account", attempt["account_hash"],
                        request_id=getattr(request.state, "request_id", ""), details={"keys": sorted(body.values), "revision": version, "attempt_id": str(body.attempt_id)})
    return {"version": version, "updated": sorted(body.values)}


@router.post("/api/v1/admin/leyu/renewal/{account_hash}/reauthorize")
async def reauthorize(account_hash: str, body: ResetInput, request: Request):
    admin = await require_admin(request)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            account = await conn.fetchrow("SELECT * FROM v2_renewal_accounts WHERE account_hash=$1 FOR UPDATE", account_hash)
            if not account:
                raise HTTPException(status_code=404, detail="续期账号不存在")
            if account["state"] == "running":
                if account["lease_until"] and account["lease_until"] > datetime.now(timezone.utc):
                    raise HTTPException(status_code=409, detail="续期仍在进行，不可重置")
                await conn.execute("UPDATE v2_renewal_attempts SET state='unknown',finished_at=now(),details=$2 WHERE id=$1 AND state='running'",
                                   account["active_attempt"], {"category": "expired_admin_reauthorization", "login_submitted": True})
            await conn.execute("UPDATE v2_renewal_accounts SET state='ready',active_attempt=NULL,lease_until=NULL,retry_after=NULL,updated_at=now() WHERE account_hash=$1", account_hash)
            await audit(conn, admin["id"], "admin", "renewal_reauthorized", "leyu_account", account_hash,
                        request_id=getattr(request.state, "request_id", ""), details={"reason": body.reason, "prior_state": account["state"]})
    return {"status": "ready"}
