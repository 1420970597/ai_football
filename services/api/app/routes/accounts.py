from __future__ import annotations

import json
import uuid
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import database
from ..auth import require_user
from ..membership import has_entitlement
from ..security import encrypt_secret

router = APIRouter(prefix="/api/v1/accounts")


class LeyuBindingInput(BaseModel):
    account_label: str = Field(min_length=1, max_length=80)
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=256)


class AutoBetInput(BaseModel):
    enabled: bool
    confirmation_phrase: str = ""
    allowed_experts: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    stake_per_order: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    max_per_order: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    daily_limit: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    open_exposure_limit: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)


@router.get("/leyu")
async def get_leyu_account(request: Request):
    user = await require_user(request)
    row = await database.current_pool().fetchrow(
        "SELECT account_label,health_state,last_verified_at,updated_at FROM v2_leyu_accounts WHERE user_id=$1",
        user["id"],
    )
    settings = await database.current_pool().fetchrow(
        "SELECT auto_enabled,allowed_experts,stake_per_order,max_per_order,daily_limit,open_exposure_limit,emergency_stopped "
        "FROM v2_user_bet_settings WHERE user_id=$1", user["id"],
    )
    return {"account": dict(row) if row else None, "betting": dict(settings) if settings else None}


@router.put("/leyu")
async def bind_leyu_account(body: LeyuBindingInput, request: Request):
    user = await require_user(request)
    settings = request.app.state.settings
    encrypted = encrypt_secret(settings.encryption_key, json.dumps({
        "username": body.username,
        "password": body.password,
    }, ensure_ascii=False, separators=(",", ":")))
    pool = database.current_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO v2_leyu_accounts(id,user_id,encrypted_credentials,account_label,health_state) "
                "VALUES($1,$2,$3,$4,'unverified') ON CONFLICT(user_id) DO UPDATE SET "
                "encrypted_credentials=EXCLUDED.encrypted_credentials,account_label=EXCLUDED.account_label,"
                "health_state='unverified',last_verified_at=NULL,updated_at=now()",
                uuid.uuid4(), user["id"], encrypted, body.account_label.strip(),
            )
            await conn.execute(
                "INSERT INTO v2_user_bet_settings(user_id) VALUES($1) ON CONFLICT(user_id) DO NOTHING", user["id"],
            )
            # 更换账户不能继承原账号的实盘许可。
            await conn.execute(
                "UPDATE v2_user_bet_settings SET auto_enabled=FALSE,emergency_stopped=TRUE,updated_at=now() WHERE user_id=$1",
                user["id"],
            )
            await conn.execute(
                "INSERT INTO v2_audit_log(id,actor_id,actor_role,action,resource_type,resource_id) "
                "VALUES($1,$2,'user','leyu_account_bound','leyu_account',$3)",
                uuid.uuid4(), user["id"], str(user["id"]),
            )
    return {
        "state": "unverified",
        "account_label": body.account_label.strip(),
        "message": "账户凭据已加密保存；完成乐鱼登录校验前不能启用自动投注。",
    }


@router.delete("/leyu")
async def unbind_leyu_account(request: Request):
    user = await require_user(request)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM v2_leyu_accounts WHERE user_id=$1", user["id"])
            await conn.execute(
                "UPDATE v2_user_bet_settings SET auto_enabled=FALSE,emergency_stopped=TRUE,updated_at=now() WHERE user_id=$1",
                user["id"],
            )
            await conn.execute(
                "INSERT INTO v2_audit_log(id,actor_id,actor_role,action,resource_type,resource_id) "
                "VALUES($1,$2,'user','leyu_account_unbound','leyu_account',$3)",
                uuid.uuid4(), user["id"], str(user["id"]),
            )
    return {"unbound": True, "auto_betting": False}


@router.put("/leyu/auto-betting")
async def configure_auto_betting(body: AutoBetInput, request: Request):
    user = await require_user(request)
    pool = database.current_pool()
    if not body.enabled:
        await pool.execute(
            "INSERT INTO v2_user_bet_settings(user_id,auto_enabled,emergency_stopped) VALUES($1,FALSE,TRUE) "
            "ON CONFLICT(user_id) DO UPDATE SET auto_enabled=FALSE,emergency_stopped=TRUE,updated_at=now()",
            user["id"],
        )
        return {"enabled": False, "emergency_stopped": True}
    if body.confirmation_phrase != "I_UNDERSTAND":
        raise HTTPException(status_code=422, detail="需要明确确认自动投注风险")
    if not body.allowed_experts:
        raise HTTPException(status_code=422, detail="至少选择一位允许自动投注的专家")
    if min(body.stake_per_order, body.max_per_order, body.daily_limit, body.open_exposure_limit) <= 0:
        raise HTTPException(status_code=422, detail="投注本金和敞口上限必须大于零")
    if body.stake_per_order > body.max_per_order or body.max_per_order > body.daily_limit or body.max_per_order > body.open_exposure_limit:
        raise HTTPException(status_code=422, detail="投注限额关系无效")
    account = await pool.fetchrow("SELECT health_state FROM v2_leyu_accounts WHERE user_id=$1", user["id"])
    if not account or account["health_state"] != "healthy":
        raise HTTPException(status_code=409, detail="乐鱼账户尚未通过健康校验")
    global_setting = await pool.fetchrow("SELECT encrypted_value FROM v2_admin_settings WHERE setting_key='auto_betting_enabled'")
    if not global_setting:
        raise HTTPException(status_code=403, detail="管理员尚未开放自动投注")
    try:
        globally_enabled = request.app.state.decrypt_secret(global_setting["encrypted_value"]).lower() == "true"
    except Exception:
        globally_enabled = False
    if not globally_enabled:
        raise HTTPException(status_code=403, detail="管理员尚未开放自动投注")
    has_betting_entitlement = await has_entitlement(user["id"], "automatic_betting")
    if not has_betting_entitlement:
        raise HTTPException(status_code=403, detail="当前会员方案未开放自动投注权益")
    if body.allowed_experts:
        allowed = await pool.fetchval(
            "SELECT count(*) FROM v2_experts WHERE id=ANY($1::uuid[]) AND enabled", body.allowed_experts,
        )
        if allowed != len(set(body.allowed_experts)):
            raise HTTPException(status_code=422, detail="关注专家列表包含无效条目")
    await pool.execute(
        "INSERT INTO v2_user_bet_settings(user_id,auto_enabled,allowed_experts,stake_per_order,max_per_order,daily_limit,"
        "open_exposure_limit,emergency_stopped,updated_at) VALUES($1,TRUE,$2,$3,$4,$5,$6,FALSE,now()) "
        "ON CONFLICT(user_id) DO UPDATE SET auto_enabled=TRUE,allowed_experts=EXCLUDED.allowed_experts,"
        "stake_per_order=EXCLUDED.stake_per_order,max_per_order=EXCLUDED.max_per_order,daily_limit=EXCLUDED.daily_limit,"
        "open_exposure_limit=EXCLUDED.open_exposure_limit,emergency_stopped=FALSE,updated_at=now()",
        user["id"], body.allowed_experts, body.stake_per_order, body.max_per_order, body.daily_limit,
        body.open_exposure_limit,
    )
    return {"enabled": True, "emergency_stopped": False, "state": "armed"}


@router.post("/leyu/emergency-stop")
async def emergency_stop(request: Request):
    user = await require_user(request)
    await database.current_pool().execute(
        "INSERT INTO v2_user_bet_settings(user_id,auto_enabled,emergency_stopped) VALUES($1,FALSE,TRUE) "
        "ON CONFLICT(user_id) DO UPDATE SET auto_enabled=FALSE,emergency_stopped=TRUE,updated_at=now()",
        user["id"],
    )
    await database.current_pool().execute(
        "INSERT INTO v2_audit_log(id,actor_id,actor_role,action,resource_type,resource_id) "
        "VALUES($1,$2,'user','emergency_stop','betting_settings',$3)",
        uuid.uuid4(), user["id"], str(user["id"]),
    )
    return {"enabled": False, "emergency_stopped": True}
