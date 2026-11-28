"""Authorize the Hermes settings proxy and persist only its sealed document."""

import uuid

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import database
from ..auth import audit, require_admin, verify_sensitive_admin_session
from .internal import _authorized

router = APIRouter(prefix="/internal/hermes-connection")
SETTING_KEY = "_hermes_connection_v1"


def control_token(request: Request) -> str:
    return getattr(request.app.state.settings, "hermes_connection_token", "")


@router.api_route("/authorize", methods=["GET", "PUT"])
async def authorize(request: Request):
    admin = await require_admin(request)
    token = control_token(request)
    if not token:
        raise HTTPException(403, "Hermes 连接管理未启用")
    if request.method == "PUT":
        async with database.current_pool().acquire() as conn, conn.transaction():
            await verify_sensitive_admin_session(conn, admin)
    return Response(status_code=204, headers={
        "X-Hermes-Connection-Token": token,
        "X-Hermes-Connection-Actor": str(admin["id"]),
    })


@router.get("/storage")
async def read(request: Request):
    _authorized(request, control_token(request))
    row = await database.current_pool().fetchrow(
        "SELECT encrypted_value,updated_at FROM v2_admin_settings WHERE setting_key=$1", SETTING_KEY
    )
    return {"sealed": row["encrypted_value"] if row else None, "updated_at": row["updated_at"] if row else None}


class SealedConnection(BaseModel):
    sealed: str = Field(pattern=r"^[A-Za-z0-9_-]{100,8192}={0,2}$")
    previous: str | None = Field(pattern=r"^[A-Za-z0-9_-]{100,8192}={0,2}$")
    revision: int = Field(ge=1)
    actor: uuid.UUID


@router.put("/storage")
async def store(body: SealedConnection, request: Request):
    _authorized(request, control_token(request))
    async with database.current_pool().acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", SETTING_KEY)
        prior = await conn.fetchval("SELECT encrypted_value FROM v2_admin_settings WHERE setting_key=$1", SETTING_KEY)
        if prior == body.sealed:
            return {"stored": True}
        if prior != body.previous:
            raise HTTPException(409, "连接配置已更新，请刷新后重试")
        await conn.execute(
            "INSERT INTO v2_admin_settings(setting_key,encrypted_value,updated_by) VALUES($1,$2,$3) "
            "ON CONFLICT(setting_key) DO UPDATE SET encrypted_value=EXCLUDED.encrypted_value,"
            "updated_by=EXCLUDED.updated_by,updated_at=now()",
            SETTING_KEY, body.sealed, body.actor,
        )
        await audit(conn, body.actor, "admin", "hermes_connection_updated", "hermes_connection",
                    str(body.revision), request.state.request_id, details={"revision": body.revision})
    return {"stored": True}
