from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from . import database
from .membership import get_membership
from .security import encrypt_secret, hash_password, new_token, token_hash, totp_step, verify_password

router = APIRouter(prefix="/api/v1")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
ADMIN_IDENTIFIER_RE = re.compile(r"^(?:[^\s@]+@[^\s@]+\.[^\s@]+|[A-Za-z0-9_.-]{3,254})$")
USER_COOKIE = "v2_session"
ADMIN_COOKIE = "v2_admin_session"
CSRF_COOKIE = "v2_csrf"
ADMIN_CSRF_COOKIE = "v2_admin_csrf"
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SESSION_LIFETIME = timedelta(days=180)
DUMMY_HASH = "scrypt$16384$8$1$" + "00" * 16 + "$" + "00" * 32


class RegisterInput(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    display_name: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=12, max_length=128)


class LoginInput(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class AdminLoginInput(LoginInput):
    password: str = Field(min_length=6, max_length=128)
    totp_code: str | None = Field(default=None, pattern=r"^[0-9]{6}$")


class ProfileInput(BaseModel):
    display_name: str = Field(min_length=1, max_length=60)


class PasswordInput(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", ""))[:128]


async def audit(conn, actor_id, actor_role: str, action: str, resource_type: str,
                resource_id: str = "", request_id: str = "", details: dict | None = None) -> None:
    await conn.execute(
        "INSERT INTO v2_audit_log(id,actor_id,actor_role,action,resource_type,resource_id,request_id,details) "
        "VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)",
        uuid.uuid4(), actor_id, actor_role, action, resource_type, resource_id, request_id[:128], details or {},
    )


async def _limit(request: Request, action: str, account: str = "", *, maximum: int = 10) -> None:
    pool = database.current_pool()
    # 只信任实际连接来源；代理地址需由入口可信配置归一，不能接受任意 X-Forwarded-For。
    address = request.client.host if request.client else "unknown"
    buckets = [(f"{action}:network:{address}", maximum * 3)]
    if account:
        buckets.append((f"{action}:account:{account}", maximum))
    now = datetime.now(timezone.utc)
    for raw, cap in buckets:
        bucket = token_hash(raw)
        row = await pool.fetchrow(
            "INSERT INTO v2_auth_rate_limits(bucket_key,window_started_at,attempts,updated_at) "
            "VALUES($1,$2,1,$2) ON CONFLICT(bucket_key) DO UPDATE SET "
            "attempts=CASE WHEN v2_auth_rate_limits.window_started_at <= $2-interval '15 minutes' "
            "THEN 1 ELSE v2_auth_rate_limits.attempts+1 END,"
            "window_started_at=CASE WHEN v2_auth_rate_limits.window_started_at <= $2-interval '15 minutes' "
            "THEN $2 ELSE v2_auth_rate_limits.window_started_at END,updated_at=$2 "
            "RETURNING attempts,window_started_at", bucket, now,
        )
        if row["attempts"] > cap:
            remaining = max(1, int((row["window_started_at"] + timedelta(minutes=15) - now).total_seconds()))
            raise HTTPException(status_code=429, detail="尝试过于频繁，请稍后重试", headers={"Retry-After": str(remaining)})


async def _hash_job(request: Request, fn, *args):
    semaphore = getattr(request.app.state, "password_semaphore", None)
    if semaphore is None:
        semaphore = asyncio.Semaphore(1)
        request.app.state.password_semaphore = semaphore
    async with semaphore:
        return await asyncio.to_thread(fn, *args)


def _display_name(value: str) -> str:
    result = value.strip()
    if not result or any(ord(char) < 32 or ord(char) == 127 for char in result):
        raise HTTPException(status_code=422, detail="显示名称格式无效")
    return result


async def bootstrap_admin(pool, settings) -> None:
    if not settings.bootstrap_admin_email or not settings.bootstrap_admin_password:
        return
    if not ADMIN_IDENTIFIER_RE.fullmatch(settings.bootstrap_admin_email) or len(settings.bootstrap_admin_password) < 6:
        raise RuntimeError("bootstrap administrator configuration is invalid")
    password_hash = await asyncio.to_thread(hash_password, settings.bootstrap_admin_password)
    totp_ciphertext = (
        encrypt_secret(settings.encryption_key, settings.bootstrap_admin_totp_secret)
        if settings.bootstrap_admin_totp_secret else None
    )
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('v2-bootstrap-admin'))")
            if await conn.fetchval("SELECT id FROM v2_users WHERE role='admin' LIMIT 1"):
                return
            if await conn.fetchval("SELECT id FROM v2_users WHERE email=$1", settings.bootstrap_admin_email):
                raise RuntimeError("bootstrap administrator address is already registered")
            admin_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO v2_users(id,email,display_name,password_hash,role,totp_secret_ciphertext) "
                "VALUES($1,$2,'管理员',$3,'admin',$4)",
                admin_id, settings.bootstrap_admin_email, password_hash, totp_ciphertext,
            )
            await audit(conn, admin_id, "system", "bootstrap_admin_created", "user", str(admin_id))


async def _create_session(request: Request, response: Response, user, session_kind: str, conn, *,
                          verified_now: bool = True, reauthenticated_at: datetime | None = None) -> dict:
    settings = request.app.state.settings
    token, csrf = new_token(), new_token()
    now = datetime.now(timezone.utc)
    expires_at = now + SESSION_LIFETIME
    await conn.execute(
        "INSERT INTO v2_sessions(token_hash,csrf_hash,user_id,session_kind,expires_at,reauthenticated_at) VALUES($1,$2,$3,$4,$5,$6)",
        token_hash(token), token_hash(csrf), user["id"], session_kind, expires_at,
        now if verified_now else reauthenticated_at,
    )
    session_cookie = ADMIN_COOKIE if session_kind == "admin" else USER_COOKIE
    csrf_cookie = ADMIN_CSRF_COOKIE if session_kind == "admin" else CSRF_COOKIE
    age = int(SESSION_LIFETIME.total_seconds())
    response.set_cookie(session_cookie, token, httponly=True, secure=settings.cookie_secure,
                        samesite="strict", path="/", max_age=age, expires=expires_at)
    response.set_cookie(csrf_cookie, csrf, httponly=False, secure=settings.cookie_secure,
                        samesite="strict", path="/", max_age=age, expires=expires_at)
    await audit(conn, user["id"], user["role"], f"{session_kind}_session_created", "session",
                token_hash(token), _request_id(request))
    # 长期会话令牌只能由 HttpOnly Cookie 接收，不出现在 JSON 或审计明文中。
    return {"csrf_token": csrf, "expires_at": expires_at.isoformat()}


def _session_context(request: Request, session_kind: str | None) -> tuple[str, str | None, bool]:
    kind = session_kind or request.headers.get("x-session-context", "user")
    if kind not in {"user", "admin"}:
        raise HTTPException(status_code=400, detail="会话上下文无效")
    authorization = request.headers.get("authorization", "")
    if authorization:
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="认证方式无效")
        return kind, authorization[7:].strip(), False
    return kind, request.cookies.get(ADMIN_COOKIE if kind == "admin" else USER_COOKIE), True


async def get_identity(request: Request, required: bool = False, session_kind: str | None = None) -> dict | None:
    kind, token, cookie_auth = _session_context(request, session_kind)
    if not token or len(token) > 256:
        if required:
            raise HTTPException(status_code=401, detail="需要登录")
        return None
    row = await database.current_pool().fetchrow(
        "SELECT u.id,u.email,u.display_name,u.role,u.disabled_at,u.closure_requested_at,s.session_kind,s.csrf_hash,s.expires_at,s.reauthenticated_at "
        "FROM v2_sessions s JOIN v2_users u ON u.id=s.user_id "
        "WHERE s.token_hash=$1 AND s.expires_at>now()",
        token_hash(token),
    )
    if not row or row["disabled_at"] is not None:
        if required:
            raise HTTPException(status_code=401, detail="会话无效或已过期")
        return None
    if row["session_kind"] != kind or row["role"] != kind:
        raise HTTPException(status_code=403, detail="会话类型不匹配")
    if cookie_auth and request.method in WRITE_METHODS:
        csrf_cookie = request.cookies.get(ADMIN_CSRF_COOKIE if kind == "admin" else CSRF_COOKIE, "")
        csrf_header = request.headers.get("x-csrf-token", "")
        if (not csrf_cookie or not csrf_header or len(csrf_header) > 256 or len(csrf_cookie) > 256
                or not hmac.compare_digest(csrf_cookie.encode(), csrf_header.encode())
                or not hmac.compare_digest(token_hash(csrf_header), row["csrf_hash"])):
            raise HTTPException(status_code=403, detail="CSRF 校验失败")
    identity = dict(row)
    identity["_token_hash"] = token_hash(token)
    await database.current_pool().execute("UPDATE v2_sessions SET last_seen_at=now() WHERE token_hash=$1 AND last_seen_at<now()-interval '1 minute'", identity["_token_hash"])
    return identity


async def require_user(request: Request) -> dict:
    identity = await get_identity(request, required=True, session_kind="user")
    stopping = (request.url.path == "/api/v1/accounts/leyu/auto-betting" and request.method == "PUT"
                and (await request.json()).get("enabled") is False)
    if (identity["closure_requested_at"] is not None and request.method in WRITE_METHODS
            and not stopping
            and request.url.path not in {"/api/v1/auth/logout", "/api/v1/auth/logout-all", "/api/v1/auth/closure",
                                         "/api/v1/auth/closure/cancel", "/api/v1/auth/reauthenticate",
                                         "/api/v1/accounts/leyu/emergency-stop"}):
        raise HTTPException(409, "账户正在注销核对中，仅可查看本人对账、停止投注或取消注销")
    return identity


async def require_admin(request: Request) -> dict:
    identity = await get_identity(request, required=True, session_kind="admin")
    if request.method in WRITE_METHODS and not request.url.path.endswith("/emergency-stop"):
        require_recent(identity)
    return identity


def require_recent(identity: dict) -> None:
    verified = identity.get("reauthenticated_at")
    if verified is None or not (datetime.now(timezone.utc) - timedelta(minutes=5) < verified <= datetime.now(timezone.utc)):
        raise HTTPException(403, detail={"code": "reauthentication_required", "session_kind": identity["session_kind"],
                                         "message": "敏感操作需要再次验证当前凭据"})


async def require_recent_user(request: Request) -> dict:
    identity = await require_user(request)
    require_recent(identity)
    return identity


async def verify_sensitive_admin_session(conn, identity: dict, *, require_fresh: bool = True):
    # Recheck after acquiring the user lock: a request may have waited while
    # another session rotated MFA or consumed a recovery code and revoked it.
    # NO KEY UPDATE permits the audit actor FK check of concurrent logout.
    user = await conn.fetchrow(
        "SELECT id,email,totp_secret_ciphertext FROM v2_users "
        "WHERE id=$1 AND role='admin' AND disabled_at IS NULL FOR NO KEY UPDATE", identity["id"],
    )
    if not user:
        raise HTTPException(401, "管理身份已失效")
    current = await conn.fetchrow(
        "SELECT reauthenticated_at FROM v2_sessions WHERE token_hash=$1 AND user_id=$2 "
        "AND session_kind='admin' AND expires_at>now() FOR UPDATE", identity["_token_hash"], identity["id"],
    )
    if not current:
        raise HTTPException(401, "管理会话已撤销或已过期")
    if require_fresh:
        require_recent({"reauthenticated_at": current["reauthenticated_at"], "session_kind": "admin"})
    return user


async def stop_betting_after_credential_change(conn, user_id, reason: str) -> None:
    # The same account row serializes credential revocation with the executor's reservation/send checks.
    from . import orders
    await conn.fetchrow("SELECT id FROM v2_leyu_accounts WHERE user_id=$1 FOR UPDATE", user_id)
    await conn.execute("UPDATE v2_user_bet_settings SET auto_enabled=FALSE,emergency_stopped=TRUE,consent_account_id=NULL,consent_binding_revision=NULL,revision=revision+1,updated_at=now() WHERE user_id=$1", user_id)
    await orders.cancel_unsent(conn, user_id, reason)


async def verify_sensitive_session(conn, identity: dict) -> None:
    current = await conn.fetchrow(
        "SELECT s.reauthenticated_at,u.closure_requested_at FROM v2_sessions s JOIN v2_users u ON u.id=s.user_id "
        "WHERE s.token_hash=$1 AND s.user_id=$2 AND s.session_kind='user' AND s.expires_at>now() AND u.disabled_at IS NULL",
        identity.get("_token_hash", ""), identity["id"],
    )
    if not current:
        raise HTTPException(401, "会话已经撤销，请重新登录")
    if current["closure_requested_at"] is not None:
        raise HTTPException(409, "账户正在注销核对中")
    require_recent({"reauthenticated_at": current["reauthenticated_at"], "session_kind": "user"})


@router.post("/auth/register", status_code=status.HTTP_201_CREATED)
async def register(body: RegisterInput, request: Request, response: Response):
    email = body.email.strip().lower()
    name = _display_name(body.display_name)
    if not EMAIL_RE.fullmatch(email):
        raise HTTPException(status_code=422, detail="邮箱格式无效")
    await _limit(request, "register", email, maximum=5)
    password = await _hash_job(request, hash_password, body.password)
    user = {"id": uuid.uuid4(), "email": email, "display_name": name, "role": "user"}
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            try:
                await conn.execute(
                    "INSERT INTO v2_users(id,email,display_name,password_hash,role) VALUES($1,$2,$3,$4,'user')",
                    user["id"], email, name, password,
                )
            except Exception as exc:
                if getattr(exc, "sqlstate", None) == "23505":
                    raise HTTPException(status_code=409, detail="该邮箱已注册") from None
                raise
            free_plan_id = await conn.fetchval("SELECT id FROM v2_membership_plans WHERE code='free' AND enabled")
            if free_plan_id:
                await conn.execute(
                    "INSERT INTO v2_memberships(id,user_id,plan_id,status,starts_at,ends_at,grant_source) "
                    "VALUES($1,$2,$3,'active',now(),now()+interval '100 years','system')",
                    uuid.uuid4(), user["id"], free_plan_id,
                )
            await audit(conn, user["id"], "user", "user_registered", "user", str(user["id"]), _request_id(request))
            session = await _create_session(request, response, user, "user", conn)
    return {"user": user, **session}


@router.post("/auth/login")
async def login(body: LoginInput, request: Request, response: Response):
    email = body.email.strip().lower()
    await _limit(request, "login", email)
    row = await database.current_pool().fetchrow(
        "SELECT id,email,display_name,role,password_hash,disabled_at FROM v2_users WHERE email=$1", email,
    )
    password_ok = await _hash_job(request, verify_password, body.password, row["password_hash"] if row else DUMMY_HASH)
    if not row or row["role"] != "user" or row["disabled_at"] is not None or not password_ok:
        raise HTTPException(status_code=401, detail="邮箱或密码无效")
    user = {key: row[key] for key in ("id", "email", "display_name", "role")}
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow("SELECT password_hash,disabled_at FROM v2_users WHERE id=$1 FOR UPDATE", row["id"])
            if not current or current["disabled_at"] is not None or not hmac.compare_digest(current["password_hash"], row["password_hash"]):
                raise HTTPException(401, "邮箱或密码无效")
            session = await _create_session(request, response, user, "user", conn)
    return {"user": user, **session}


@router.post("/admin/login")
async def admin_login(body: AdminLoginInput, request: Request, response: Response):
    email = body.email.strip().lower()
    if not ADMIN_IDENTIFIER_RE.fullmatch(email):
        raise HTTPException(status_code=422, detail="管理员账号格式无效")
    await _limit(request, "admin_login", email, maximum=5)
    row = await database.current_pool().fetchrow(
        "SELECT id,email,display_name,role,password_hash,totp_secret_ciphertext,disabled_at "
        "FROM v2_users WHERE email=$1 AND role='admin'", email,
    )
    password_ok = await _hash_job(request, verify_password, body.password, row["password_hash"] if row else DUMMY_HASH)
    if not row or row["disabled_at"] is not None or not password_ok:
        raise HTTPException(status_code=401, detail="管理员凭据无效")
    step = None
    if row["totp_secret_ciphertext"]:
        try:
            secret = request.app.state.decrypt_secret(row["totp_secret_ciphertext"])
        except Exception:
            raise HTTPException(status_code=503, detail="管理员二次验证配置无法读取") from None
        step = totp_step(secret, body.totp_code)
        if step is None:
            raise HTTPException(status_code=401, detail="动态验证码无效")
    user = {key: row[key] for key in ("id", "email", "display_name", "role")}
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT password_hash,disabled_at,totp_secret_ciphertext,totp_last_step "
                "FROM v2_users WHERE id=$1 FOR UPDATE", row["id"],
            )
            if (
                not current or current["disabled_at"] is not None
                or not hmac.compare_digest(current["password_hash"], row["password_hash"])
                or (current["totp_secret_ciphertext"] or "") != (row["totp_secret_ciphertext"] or "")
            ):
                raise HTTPException(status_code=401, detail="管理员凭据或验证器已变更")
            if step is not None:
                if current["totp_last_step"] is not None and current["totp_last_step"] >= step:
                    raise HTTPException(status_code=401, detail="动态验证码无效或已使用")
                await conn.execute("UPDATE v2_users SET totp_last_step=$2 WHERE id=$1", row["id"], step)
            session = await _create_session(request, response, user, "admin", conn)
    return {"user": user, **session}


async def _me(request: Request, kind: str):
    identity = await get_identity(request, required=True, session_kind=kind)
    membership = await get_membership(identity["id"]) if kind == "user" else None
    return {"user": {key: identity[key] for key in ("id", "email", "display_name", "role")},
            "membership": membership, "session_kind": kind, "expires_at": identity["expires_at"],
            "reauthenticated_until": identity["reauthenticated_at"] + timedelta(minutes=5) if identity["reauthenticated_at"] else None,
            "closure_requested_at": identity["closure_requested_at"]}


@router.get("/auth/me")
async def me(request: Request):
    return await _me(request, "user")


@router.get("/admin/me")
async def admin_me(request: Request):
    return await _me(request, "admin")


async def _logout(request: Request, response: Response, kind: str):
    identity = await get_identity(request, session_kind=kind)
    if identity:
        async with database.current_pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM v2_sessions WHERE token_hash=$1", identity["_token_hash"])
                await audit(conn, identity["id"], kind, f"{kind}_logout", "session", identity["_token_hash"], _request_id(request))
    names = (ADMIN_COOKIE, ADMIN_CSRF_COOKIE) if kind == "admin" else (USER_COOKIE, CSRF_COOKIE)
    for name in names:
        response.delete_cookie(name, path="/", secure=request.app.state.settings.cookie_secure, samesite="strict")
    return {"ok": True, "session_kind": kind}


@router.post("/auth/logout")
async def logout(request: Request, response: Response):
    return await _logout(request, response, "user")


@router.post("/admin/logout")
async def admin_logout(request: Request, response: Response):
    return await _logout(request, response, "admin")


@router.post("/auth/logout-all")
async def logout_all(request: Request, response: Response):
    identity = await require_user(request)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM v2_sessions WHERE user_id=$1 AND session_kind='user'", identity["id"])
            await audit(conn, identity["id"], "user", "user_logout_all", "user", str(identity["id"]), _request_id(request))
    for name in (USER_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/", secure=request.app.state.settings.cookie_secure, samesite="strict")
    return {"ok": True}


@router.post("/auth/refresh")
async def refresh(request: Request, response: Response):
    identity = await require_user(request)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            removed = await conn.fetchval("DELETE FROM v2_sessions WHERE token_hash=$1 RETURNING token_hash", identity["_token_hash"])
            if not removed:
                raise HTTPException(status_code=401, detail="会话已撤销")
            result = await _create_session(request, response, identity, "user", conn,
                                           verified_now=False, reauthenticated_at=identity["reauthenticated_at"])
    return result


@router.patch("/auth/profile")
async def profile(body: ProfileInput, request: Request):
    identity = await require_user(request)
    name = _display_name(body.display_name)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE v2_users SET display_name=$2,updated_at=now() WHERE id=$1", identity["id"], name)
            await audit(conn, identity["id"], "user", "profile_updated", "user", str(identity["id"]), _request_id(request))
    return {"display_name": name}


@router.put("/auth/password")
async def change_password(body: PasswordInput, request: Request, response: Response):
    identity = await require_user(request)
    await _limit(request, "change_password", str(identity["id"]), maximum=5)
    old = await database.current_pool().fetchval("SELECT password_hash FROM v2_users WHERE id=$1", identity["id"])
    if not await _hash_job(request, verify_password, body.current_password, old):
        raise HTTPException(status_code=401, detail="当前密码无效")
    replacement = await _hash_job(request, hash_password, body.new_password)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            changed = await conn.fetchval(
                "UPDATE v2_users SET password_hash=$2,updated_at=now() "
                "WHERE id=$1 AND password_hash=$3 RETURNING id", identity["id"], replacement, old,
            )
            if not changed:
                raise HTTPException(status_code=409, detail="凭据已经变更，请重新登录")
            await conn.execute("DELETE FROM v2_sessions WHERE user_id=$1", identity["id"])
            await stop_betting_after_credential_change(conn, identity["id"], "platform_password_changed")
            await audit(conn, identity["id"], "user", "password_changed", "user", str(identity["id"]), _request_id(request))
    for name in (USER_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/", secure=request.app.state.settings.cookie_secure, samesite="strict")
    return {"ok": True, "reauthentication_required": True, "auto_betting": False}
