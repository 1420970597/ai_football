"""管理员验证器换机：确认新因子前保持旧因子，按会话绑定加密暂存。"""
from __future__ import annotations

import base64
import hmac
import secrets
import uuid
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from . import auth, database
from .security import encrypt_secret, new_token, token_hash, totp_step

router = APIRouter(prefix="/api/v1/admin/mfa")


class EnrollmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    replace_existing: bool = False


class ConfirmInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    totp_code: str = Field(pattern=r"^[0-9]{6}$")


@router.get("/status")
async def status(request: Request):
    admin = await auth.get_identity(request, required=True, session_kind="admin")
    encrypted = await database.current_pool().fetchval("SELECT totp_secret_ciphertext FROM v2_users WHERE id=$1", admin["id"])
    state = "missing"
    if encrypted:
        try:
            secret = request.app.state.decrypt_secret(encrypted).upper()
            decoded = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
            state = "configured" if len(decoded) >= 10 else "unreadable"
        except Exception:
            state = "unreadable"
    remaining = await database.current_pool().fetchval("SELECT count(*) FROM v2_admin_recovery_codes WHERE user_id=$1 AND consumed_at IS NULL AND revoked_at IS NULL", admin["id"])
    pending = await database.current_pool().fetchrow("SELECT id AS enrollment_id,expires_at FROM v2_admin_mfa_enrollments WHERE user_id=$1 AND session_hash=$2 AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at>now()", admin["id"], admin["_token_hash"])
    return {"configured": state == "configured", "configuration_state": state, "recovery_codes_remaining": remaining,
            "pending": dict(pending) if pending else None}


@router.post("/enrollment", status_code=201)
async def start_enrollment(body: EnrollmentInput, request: Request):
    admin = await auth.require_admin(request)
    await auth._limit(request, "admin_mfa_enrollment", str(admin["id"]), maximum=5)
    secret = base64.b32encode(secrets.token_bytes(20)).decode("ascii")
    encrypted = encrypt_secret(request.app.state.settings.encryption_key, secret)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            user = await auth.verify_sensitive_admin_session(conn, admin)
            pending = await conn.fetchrow("SELECT id,expires_at FROM v2_admin_mfa_enrollments WHERE user_id=$1 AND consumed_at IS NULL AND revoked_at IS NULL FOR UPDATE", admin["id"])
            if pending and pending["expires_at"] > datetime.now(timezone.utc) and not body.replace_existing:
                raise HTTPException(409, "已有尚未确认的新验证器，请先取消或明确替换；原验证器仍有效")
            await conn.execute("UPDATE v2_admin_mfa_enrollments SET revoked_at=now(),encrypted_secret='' WHERE user_id=$1 AND consumed_at IS NULL AND revoked_at IS NULL", admin["id"])
            row = await conn.fetchrow("INSERT INTO v2_admin_mfa_enrollments(id,user_id,session_hash,encrypted_secret,previous_secret_hash,expires_at) VALUES($1,$2,$3,$4,$5,now()+interval '10 minutes') RETURNING id,expires_at", uuid.uuid4(), admin["id"], admin["_token_hash"], encrypted, token_hash(user["totp_secret_ciphertext"] or ""))
            await auth.audit(conn, admin["id"], "admin", "admin_mfa_enrollment_started", "mfa_enrollment", str(row["id"]), auth._request_id(request), {"previous_factor_unchanged": True})
    uri = "otpauth://totp/" + quote("球讯:" + admin["email"], safe="") + "?" + urlencode({"secret": secret, "issuer": "球讯", "algorithm": "SHA1", "digits": 6, "period": 30})
    return {"enrollment_id": row["id"], "secret": secret, "otpauth_uri": uri, "expires_at": row["expires_at"],
            "message": "请在新验证器登记并输入新验证码。确认成功前，原验证器和备用码继续有效。此密钥仅显示一次。"}


@router.post("/enrollment/{enrollment_id}/confirm")
async def confirm_enrollment(enrollment_id: uuid.UUID, body: ConfirmInput, request: Request):
    admin = await auth.require_admin(request)
    await auth._limit(request, "admin_mfa_confirm", str(admin["id"]), maximum=5)
    codes = [new_token() for _ in range(10)]
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            user = await auth.verify_sensitive_admin_session(conn, admin)
            pending = await conn.fetchrow("SELECT * FROM v2_admin_mfa_enrollments WHERE id=$1 AND user_id=$2 AND session_hash=$3 FOR UPDATE", enrollment_id, admin["id"], admin["_token_hash"])
            if not pending:
                raise HTTPException(404, "当前会话没有该验证器登记请求")
            if pending["consumed_at"] is not None:
                return {"configured": True, "already_confirmed": True, "recovery_codes": None, "other_sessions_revoked": True,
                        "message": "该验证器已确认。备用码仅首次返回；如未保存，请使用当前会话重新生成备用码。"}
            if pending["revoked_at"] is not None or pending["expires_at"] <= datetime.now(timezone.utc):
                raise HTTPException(409, "登记请求已过期或已撤销；原验证器未被修改")
            if not hmac.compare_digest(token_hash(user["totp_secret_ciphertext"] or ""), pending["previous_secret_hash"]):
                raise HTTPException(409, "验证器已在另一会话变更，请重新开始登记")
            try:
                secret = request.app.state.decrypt_secret(pending["encrypted_secret"])
            except Exception:
                raise HTTPException(503, "待确认验证器无法读取，原验证器保持有效") from None
            step = totp_step(secret, body.totp_code)
            if step is None:
                raise HTTPException(401, "新验证器动态验证码无效，原验证器保持有效")
            await conn.execute("UPDATE v2_users SET totp_secret_ciphertext=$2,totp_last_step=$3,updated_at=now() WHERE id=$1", admin["id"], pending["encrypted_secret"], step)
            await conn.execute("UPDATE v2_admin_mfa_enrollments SET consumed_at=now(),encrypted_secret='' WHERE id=$1", enrollment_id)
            await conn.execute("UPDATE v2_admin_recovery_codes SET revoked_at=now() WHERE user_id=$1 AND consumed_at IS NULL AND revoked_at IS NULL", admin["id"])
            await conn.executemany("INSERT INTO v2_admin_recovery_codes(id,user_id,code_hash) VALUES($1,$2,$3)", [(uuid.uuid4(), admin["id"], token_hash(code)) for code in codes])
            await conn.execute("DELETE FROM v2_sessions WHERE user_id=$1 AND session_kind='admin' AND token_hash<>$2", admin["id"], admin["_token_hash"])
            await conn.execute("UPDATE v2_sessions SET reauthenticated_at=now(),last_seen_at=now() WHERE token_hash=$1", admin["_token_hash"])
            await auth.audit(conn, admin["id"], "admin", "admin_mfa_changed", "mfa_enrollment", str(enrollment_id), auth._request_id(request), {"other_admin_sessions_revoked": True, "old_recovery_codes_revoked": True, "new_recovery_code_count": len(codes)})
    return {"configured": True, "already_confirmed": False, "recovery_codes": codes, "other_sessions_revoked": True,
            "message": "新验证器已生效，其他管理会话和旧备用码已撤销。请离线保存这十个新备用码；当前验证码已用于确认，下次登录等待下一枚验证码。"}


@router.delete("/enrollment/{enrollment_id}")
async def cancel_enrollment(enrollment_id: uuid.UUID, request: Request):
    # Cancelling an unconfirmed factor never changes the old factor; allow it after step-up expires.
    admin = await auth.get_identity(request, required=True, session_kind="admin")
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            await auth.verify_sensitive_admin_session(conn, admin, require_fresh=False)
            pending = await conn.fetchrow("SELECT consumed_at,revoked_at FROM v2_admin_mfa_enrollments WHERE id=$1 AND user_id=$2 AND session_hash=$3 FOR UPDATE", enrollment_id, admin["id"], admin["_token_hash"])
            if not pending:
                raise HTTPException(404, "当前会话没有该验证器登记请求")
            if pending["consumed_at"] is not None:
                raise HTTPException(409, "已确认的验证器不能按待确认请求取消")
            if pending["revoked_at"] is None:
                await conn.execute("UPDATE v2_admin_mfa_enrollments SET revoked_at=now(),encrypted_secret='' WHERE id=$1", enrollment_id)
                await auth.audit(conn, admin["id"], "admin", "admin_mfa_enrollment_cancelled", "mfa_enrollment", str(enrollment_id), auth._request_id(request), {"previous_factor_unchanged": True})
    return {"cancelled": True, "previous_factor_unchanged": True}
