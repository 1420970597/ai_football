"""近期凭据校验、人工辅助恢复及本人数据生命周期；不提供伪造发送渠道。"""
from __future__ import annotations

import hmac
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from . import auth, database, orders
from .security import hash_password, new_token, token_hash, totp_step, verify_password

router = APIRouter(prefix="/api/v1")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Reauthenticate(Input):
    password: str = Field(min_length=1, max_length=128)


class AdminReauthenticate(Reauthenticate):
    password: str = Field(min_length=6, max_length=128)
    totp_code: str | None = Field(default=None, pattern=r"^[0-9]{6}$")


class IssueRecovery(Input):
    reason: str = Field(min_length=3, max_length=500)
    idempotency_key: uuid.UUID


class CompleteRecovery(Input):
    email: str = Field(min_length=3, max_length=254)
    code: str = Field(min_length=20, max_length=160)
    new_password: str = Field(min_length=12, max_length=128)


class RecoveryLogin(Input):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)
    recovery_code: str = Field(min_length=20, max_length=160)


class Closure(Reauthenticate):
    confirmation_phrase: str = Field(max_length=60)


async def password_matches(request, identity, password):
    row = await database.current_pool().fetchrow("SELECT password_hash,disabled_at FROM v2_users WHERE id=$1", identity["id"])
    ok = await auth._hash_job(request, verify_password, password, row["password_hash"] if row else auth.DUMMY_HASH)
    if not row or row["disabled_at"] is not None or not ok:
        raise HTTPException(401, "当前凭据无效")
    return row["password_hash"]


async def finish_reauthentication(
    request, identity, expected_password, *, step=None, expected_secret=None,
    verify_totp_configuration: bool = False,
):
    now = datetime.now(timezone.utc)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow("SELECT password_hash,disabled_at,totp_last_step,totp_secret_ciphertext FROM v2_users WHERE id=$1 FOR UPDATE", identity["id"])
            if not user or user["disabled_at"] is not None or not hmac.compare_digest(user["password_hash"], expected_password):
                raise HTTPException(401, "凭据已经变更，请重新登录")
            if verify_totp_configuration and (user["totp_secret_ciphertext"] or "") != (expected_secret or ""):
                raise HTTPException(401, "管理员验证器已经变更，请重新验证")
            if step is not None:
                if expected_secret is None or not hmac.compare_digest(user["totp_secret_ciphertext"] or "", expected_secret):
                    raise HTTPException(401, "验证器已经变更，请使用当前验证器重新验证")
                if user["totp_last_step"] is not None and user["totp_last_step"] >= step:
                    raise HTTPException(401, "动态验证码无效或已使用")
                await conn.execute("UPDATE v2_users SET totp_last_step=$2 WHERE id=$1", identity["id"], step)
            changed = await conn.fetchval("UPDATE v2_sessions SET reauthenticated_at=$2 WHERE token_hash=$1 AND expires_at>now() RETURNING token_hash", identity["_token_hash"], now)
            if not changed:
                raise HTTPException(401, "会话已撤销，请重新登录")
            await auth.audit(conn, identity["id"], identity["role"], "session_reauthenticated", "session", identity["_token_hash"], auth._request_id(request))
    return {"verified": True, "session_kind": identity["session_kind"], "expires_at": now + timedelta(minutes=5)}


@router.post("/auth/reauthenticate")
async def reauthenticate(body: Reauthenticate, request: Request):
    user = await auth.require_user(request)
    await auth._limit(request, "reauthenticate", str(user["id"]), maximum=5)
    password = await password_matches(request, user, body.password)
    return await finish_reauthentication(request, user, password)


@router.post("/admin/reauthenticate")
async def admin_reauthenticate(body: AdminReauthenticate, request: Request):
    admin = await auth.get_identity(request, required=True, session_kind="admin")
    await auth._limit(request, "admin_reauthenticate", str(admin["id"]), maximum=5)
    password = await password_matches(request, admin, body.password)
    encrypted = await database.current_pool().fetchval("SELECT totp_secret_ciphertext FROM v2_users WHERE id=$1", admin["id"])
    step = None
    if encrypted:
        try:
            secret = request.app.state.decrypt_secret(encrypted)
        except Exception:
            raise HTTPException(503, "管理员二次验证配置无法读取") from None
        step = totp_step(secret, body.totp_code)
        if step is None:
            raise HTTPException(401, "动态验证码无效")
    return await finish_reauthentication(
        request, admin, password, step=step, expected_secret=encrypted,
        verify_totp_configuration=True,
    )


@router.get("/auth/recovery/status")
async def recovery_status():
    return {"delivery_configured": False, "self_service_available": False, "mode": "administrator_assisted",
            "message": "未配置自动恢复发送渠道，请联系管理员核验身份后获取一次性恢复码。"}


@router.post("/auth/recovery/request")
async def recovery_request(request: Request):
    await auth._limit(request, "recovery_request", maximum=3)
    raise HTTPException(503, "未配置恢复发送渠道，请联系管理员；系统没有发送邮件或消息。")


@router.post("/admin/users/{user_id}/password-recovery", status_code=201)
async def issue_password_recovery(user_id: uuid.UUID, body: IssueRecovery, request: Request):
    admin = await auth.require_admin(request)
    await auth._limit(request, "issue_recovery", str(admin["id"]), maximum=10)
    reason = body.reason.strip()
    if len(reason) < 3:
        raise HTTPException(422, "请记录已核验身份及恢复原因")
    code = new_token()
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"password-recovery:{body.idempotency_key}")
            await auth.verify_sensitive_admin_session(conn, admin)
            user = await conn.fetchrow("SELECT id,role,disabled_at,closure_requested_at FROM v2_users WHERE id=$1 FOR UPDATE", user_id)
            if not user or user["role"] != "user" or user["disabled_at"] is not None or user["closure_requested_at"] is not None:
                raise HTTPException(404, "没有可恢复的普通用户账户")
            existing = await conn.fetchrow("SELECT * FROM v2_password_recoveries WHERE idempotency_key=$1", body.idempotency_key)
            if existing:
                if existing["user_id"] != user_id or existing["issued_by"] != admin["id"] or existing["reason"] != reason:
                    raise HTTPException(409, "幂等编号已用于其他恢复请求")
                return {"recovery_id": existing["id"], "expires_at": existing["expires_at"], "code": None, "replayed": True,
                        "message": "一次性恢复码只在首次签发时显示；遗失后需使用新请求编号重新签发。"}
            await conn.execute("UPDATE v2_password_recoveries SET revoked_at=now() WHERE user_id=$1 AND consumed_at IS NULL AND revoked_at IS NULL", user_id)
            row = await conn.fetchrow("INSERT INTO v2_password_recoveries(id,user_id,code_hash,issued_by,idempotency_key,reason,expires_at) VALUES($1,$2,$3,$4,$5,$6,now()+interval '10 minutes') RETURNING id,expires_at",
                                     uuid.uuid4(), user_id, token_hash(code), admin["id"], body.idempotency_key, reason)
            await auth.audit(conn, admin["id"], "admin", "password_recovery_issued", "user", str(user_id), auth._request_id(request),
                             {"reason": reason, "recovery_id": str(row["id"]), "delivery": "manual_identity_verified"})
    return {"recovery_id": row["id"], "expires_at": row["expires_at"], "code": code, "replayed": False,
            "message": "请通过已核验渠道人工交付；系统没有自动发送，此码仅显示一次。"}


@router.post("/auth/recovery/complete")
async def complete_recovery(body: CompleteRecovery, request: Request, response: Response):
    email = body.email.strip().lower()
    await auth._limit(request, "complete_recovery", email, maximum=5)
    # Do expensive hashing only after the bounded credential lookup accepts a live code.
    row = await database.current_pool().fetchrow("SELECT r.id,r.user_id FROM v2_password_recoveries r JOIN v2_users u ON u.id=r.user_id WHERE u.email=$1 AND u.role='user' AND u.disabled_at IS NULL AND u.closure_requested_at IS NULL AND r.code_hash=$2 AND r.expires_at>now() AND r.consumed_at IS NULL AND r.revoked_at IS NULL", email, token_hash(body.code))
    if not row:
        raise HTTPException(401, "恢复码无效、已使用或已过期")
    password = await auth._hash_job(request, hash_password, body.new_password)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            await conn.fetchrow("SELECT id FROM v2_users WHERE id=$1 FOR UPDATE", row["user_id"])
            accepted = await conn.fetchval("UPDATE v2_password_recoveries SET consumed_at=now() WHERE id=$1 AND expires_at>now() AND consumed_at IS NULL AND revoked_at IS NULL RETURNING id", row["id"])
            if not accepted:
                raise HTTPException(401, "恢复码无效、已使用或已过期")
            updated = await conn.fetchval("UPDATE v2_users SET password_hash=$2,updated_at=now() WHERE id=$1 AND disabled_at IS NULL AND closure_requested_at IS NULL RETURNING id", row["user_id"], password)
            if not updated:
                raise HTTPException(401, "恢复码无效、已使用或已过期")
            await conn.execute("UPDATE v2_password_recoveries SET revoked_at=now() WHERE user_id=$1 AND consumed_at IS NULL AND revoked_at IS NULL", row["user_id"])
            await conn.execute("DELETE FROM v2_sessions WHERE user_id=$1", row["user_id"])
            await auth.stop_betting_after_credential_change(conn, row["user_id"], "platform_password_recovered")
            await auth.audit(conn, row["user_id"], "user", "password_recovered", "user", str(row["user_id"]), auth._request_id(request), {"recovery_id": str(row["id"])})
    for name in (auth.USER_COOKIE, auth.CSRF_COOKIE):
        response.delete_cookie(name, path="/", secure=request.app.state.settings.cookie_secure, samesite="strict")
    return {"reset": True, "reauthentication_required": True, "auto_betting": False}


@router.post("/admin/mfa/recovery-codes")
async def create_admin_recovery_codes(request: Request):
    admin = await auth.require_admin(request)
    await auth._limit(request, "admin_recovery_generate", str(admin["id"]), maximum=3)
    codes = [new_token() for _ in range(10)]
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            await auth.verify_sensitive_admin_session(conn, admin)
            await conn.execute("UPDATE v2_admin_recovery_codes SET revoked_at=now() WHERE user_id=$1 AND consumed_at IS NULL AND revoked_at IS NULL", admin["id"])
            await conn.executemany("INSERT INTO v2_admin_recovery_codes(id,user_id,code_hash) VALUES($1,$2,$3)", [(uuid.uuid4(), admin["id"], token_hash(code)) for code in codes])
            await auth.audit(conn, admin["id"], "admin", "admin_recovery_codes_rotated", "user", str(admin["id"]), auth._request_id(request), {"count": len(codes)})
    return {"codes": codes, "message": "只显示一次，请离线保管。每个代码仅可使用一次，旧代码已经撤销。"}


@router.post("/admin/recovery-login")
async def admin_recovery_login(body: RecoveryLogin, request: Request, response: Response):
    email = body.email.strip().lower()
    await auth._limit(request, "admin_recovery_login", email, maximum=5)
    row = await database.current_pool().fetchrow("SELECT id,email,display_name,role,password_hash,disabled_at FROM v2_users WHERE email=$1 AND role='admin'", email)
    ok = await auth._hash_job(request, verify_password, body.password, row["password_hash"] if row else auth.DUMMY_HASH)
    if not row or row["disabled_at"] is not None or not ok:
        raise HTTPException(401, "管理员凭据无效")
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow("SELECT password_hash,disabled_at FROM v2_users WHERE id=$1 FOR UPDATE", row["id"])
            if current["disabled_at"] is not None or not hmac.compare_digest(current["password_hash"], row["password_hash"]):
                raise HTTPException(401, "管理员凭据无效")
            accepted = await conn.fetchval("UPDATE v2_admin_recovery_codes SET consumed_at=now() WHERE user_id=$1 AND code_hash=$2 AND consumed_at IS NULL AND revoked_at IS NULL RETURNING id", row["id"], token_hash(body.recovery_code))
            if not accepted:
                raise HTTPException(401, "管理员恢复码无效或已使用")
            await conn.execute("DELETE FROM v2_sessions WHERE user_id=$1 AND session_kind='admin'", row["id"])
            session = await auth._create_session(request, response, row, "admin", conn)
            await auth.audit(conn, row["id"], "admin", "admin_recovery_login", "user", str(row["id"]), auth._request_id(request), {"recovery_code_id": str(accepted)})
    return {"user": {name: row[name] for name in ("id", "email", "display_name", "role")}, **session}


def export_cursor(section, value):
    if value is None:
        return None, None
    try:
        stamp, identifier = value.split("~", 1)
        parsed = datetime.fromisoformat(stamp)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed, uuid.UUID(identifier)
    except (ValueError, TypeError):
        raise HTTPException(422, "导出游标无效") from None


@router.get("/auth/export")
async def export_own_data(request: Request, section: str = Query(default="profile", pattern="^(profile|memberships|orders)$"),
                          cursor: str | None = Query(default=None, max_length=140), limit: int = Query(default=100, ge=1, le=200)):
    user = await auth.require_recent_user(request)
    await auth._limit(request, "data_export", str(user["id"]), maximum=100)
    stamp, identifier = export_cursor(section, cursor)
    pool = database.current_pool()
    next_cursor = None
    async with pool.acquire() as conn:
        async with conn.transaction():
            if section == "profile":
                if cursor:
                    raise HTTPException(422, "个人资料不使用分页游标")
                profile = await conn.fetchrow("SELECT id,email,display_name,created_at,updated_at,closure_requested_at FROM v2_users WHERE id=$1", user["id"])
                account = await conn.fetchrow("SELECT id,account_label,health_state,last_verified_at,updated_at FROM v2_leyu_accounts WHERE user_id=$1", user["id"])
                policy = await conn.fetchrow("SELECT auto_enabled,allowed_experts,stake_per_order,max_per_order,daily_limit,open_exposure_limit,emergency_stopped,updated_at FROM v2_user_bet_settings WHERE user_id=$1", user["id"])
                items = [{"profile": dict(profile), "leyu_account": dict(account) if account else None, "betting_policy": dict(policy) if policy else None}]
            elif section == "memberships":
                rows = await conn.fetch("SELECT m.id,m.status,m.starts_at,m.ends_at,m.created_at,m.grant_source,p.code,p.name,p.price,p.currency,p.entitlements FROM v2_memberships m JOIN v2_membership_plans p ON p.id=m.plan_id WHERE m.user_id=$1 AND ($2::timestamptz IS NULL OR (m.created_at,m.id)<($2,$3::uuid)) ORDER BY m.created_at DESC,m.id DESC LIMIT $4", user["id"], stamp, identifier, limit + 1)
                items = [dict(row) for row in rows[:limit]]
                if len(rows) > limit:
                    next_cursor = items[-1]["created_at"].isoformat() + "~" + str(items[-1]["id"])
            else:
                rows = await conn.fetch("SELECT id,signal_id,account_id,state,stake,price,reserved_stake,provider_order_no,submitted_at,settled_net,settled_at,created_at,updated_at,provider_price,provider_stake,provider_payout,provider_currency,provider_fees,provider_placed_at,receipt_observed_at,pnl_basis FROM v2_bet_orders WHERE user_id=$1 AND ($2::timestamptz IS NULL OR (created_at,id)<($2,$3::uuid)) ORDER BY created_at DESC,id DESC LIMIT $4", user["id"], stamp, identifier, limit + 1)
                items = [dict(row) for row in rows[:limit]]
                if len(rows) > limit:
                    next_cursor = items[-1]["created_at"].isoformat() + "~" + str(items[-1]["id"])
            await auth.audit(conn, user["id"], "user", "personal_data_exported", "user", str(user["id"]), auth._request_id(request), {"section": section, "count": len(items)})
    return {"schema_version": 1, "section": section, "generated_at": datetime.now(timezone.utc), "items": items, "next_cursor": next_cursor,
            "excluded": ["passwords", "session_tokens", "provider_credentials", "internal_security_records"]}


@router.get("/auth/closure")
async def closure_status(request: Request):
    user = await auth.require_user(request)
    outstanding = await database.current_pool().fetchval("SELECT count(*) FROM v2_bet_orders WHERE user_id=$1 AND state IN('pending','submitting','accepted','unknown')", user["id"])
    return {"state": "pending_accounting" if user["closure_requested_at"] else "not_requested", "requested_at": user["closure_requested_at"],
            "outstanding_orders": outstanding, "retention": "订单、成交、结算及审计证据保留用于对账；可注销后移除登录身份及第三方凭据。",
            "message": "注销先停止新投注。存在未知或未结算订单时保留对账权限及必要账号连接，核对结束后再次确认完成注销。"}


@router.post("/auth/closure")
async def close_own_account(body: Closure, request: Request, response: Response):
    user = await auth.require_user(request)
    await auth._limit(request, "account_closure", str(user["id"]), maximum=5)
    if body.confirmation_phrase != "注销我的球讯账户":
        raise HTTPException(422, "请明确确认注销本人平台账户")
    expected = await password_matches(request, user, body.password)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow("SELECT password_hash,disabled_at FROM v2_users WHERE id=$1 FOR UPDATE", user["id"])
            if not current or current["disabled_at"] is not None or not hmac.compare_digest(current["password_hash"], expected):
                raise HTTPException(401, "当前凭据已经变更")
            # Match the executor's account -> policy -> order lock order before disabling or erasing credentials.
            await conn.fetchrow("SELECT id FROM v2_leyu_accounts WHERE user_id=$1 FOR UPDATE", user["id"])
            await conn.execute("INSERT INTO v2_user_bet_settings(user_id,auto_enabled,emergency_stopped) VALUES($1,FALSE,TRUE) ON CONFLICT(user_id) DO UPDATE SET auto_enabled=FALSE,emergency_stopped=TRUE,revision=v2_user_bet_settings.revision+1,consent_account_id=NULL,consent_binding_revision=NULL,updated_at=now()", user["id"])
            await orders.cancel_unsent(conn, user["id"], "account_closure_requested")
            await conn.execute("UPDATE v2_users SET closure_requested_at=COALESCE(closure_requested_at,now()),updated_at=now() WHERE id=$1", user["id"])
            outstanding = await conn.fetchval("SELECT count(*) FROM v2_bet_orders WHERE user_id=$1 AND state IN('pending','submitting','accepted','unknown')", user["id"])
            running = await conn.fetchval("SELECT count(*) FROM v2_account_jobs WHERE user_id=$1 AND state='running'", user["id"])
            if outstanding or running:
                await auth.audit(conn, user["id"], "user", "account_closure_pending_accounting", "user", str(user["id"]), auth._request_id(request), {"outstanding_orders": outstanding, "running_jobs": running})
                response.status_code = 202
                return {"state": "pending_accounting", "outstanding_orders": outstanding, "running_jobs": running,
                        "message": "新投注已关闭。保留必要连接核对未知与未结算记录，核对完成后再次确认注销。"}
            await conn.execute("UPDATE v2_account_jobs SET state='failed',last_error='account_closed',updated_at=now() WHERE user_id=$1 AND state='queued'", user["id"])
            await conn.execute("UPDATE v2_leyu_accounts SET encrypted_credentials='',encrypted_session=NULL,provider_identity_hash=NULL,account_label='已注销账户',available_balance=NULL,balance_observed_at=NULL,health_state='disabled',config_revision=config_revision+1,binding_revision=binding_revision+1,renewal_state='ready',renewal_attempt=NULL,renewal_lease_until=NULL,updated_at=now() WHERE user_id=$1", user["id"])
            await conn.execute("UPDATE v2_memberships SET status='cancelled' WHERE user_id=$1 AND status='active'", user["id"])
            await conn.execute("DELETE FROM v2_user_bet_settings WHERE user_id=$1", user["id"])
            await conn.execute("UPDATE v2_password_recoveries SET revoked_at=now() WHERE user_id=$1 AND consumed_at IS NULL AND revoked_at IS NULL", user["id"])
            await conn.execute("UPDATE v2_users SET email=$2,display_name='已注销用户',password_hash='disabled',disabled_at=now(),anonymized_at=now(),updated_at=now() WHERE id=$1", user["id"], f"closed-{user['id']}@deleted.invalid")
            await conn.execute("DELETE FROM v2_sessions WHERE user_id=$1", user["id"])
            await auth.audit(conn, user["id"], "user", "account_anonymized", "user", str(user["id"]), auth._request_id(request), {"accounting_records_retained": True})
    for name in (auth.USER_COOKIE, auth.CSRF_COOKIE):
        response.delete_cookie(name, path="/", secure=request.app.state.settings.cookie_secure, samesite="strict")
    return {"state": "anonymized", "message": "登录身份和第三方凭据已清除。订单、结算和审计证据继续保留用于对账。"}


@router.post("/auth/closure/cancel")
async def cancel_closure(body: Reauthenticate, request: Request):
    user = await auth.require_user(request)
    await auth._limit(request, "cancel_closure", str(user["id"]), maximum=5)
    expected = await password_matches(request, user, body.password)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            changed = await conn.fetchval("UPDATE v2_users SET closure_requested_at=NULL,updated_at=now() WHERE id=$1 AND password_hash=$2 AND disabled_at IS NULL RETURNING id", user["id"], expected)
            if not changed:
                raise HTTPException(401, "当前凭据已经变更")
            await auth.audit(conn, user["id"], "user", "account_closure_cancelled", "user", str(user["id"]), auth._request_id(request), {"auto_betting_remains_disabled": True})
    return {"state": "not_requested", "auto_betting": False}
