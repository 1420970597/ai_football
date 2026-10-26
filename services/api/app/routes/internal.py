from __future__ import annotations

import hmac
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, Field

from .. import database

router = APIRouter(prefix="/internal")


@router.get("/collector/config")
async def collector_config(request: Request):
    _authorized(request, request.app.state.settings.collector_token)
    rows = await database.current_pool().fetch(
        "SELECT setting_key,encrypted_value FROM v2_admin_settings "
        "WHERE setting_key = ANY($1::text[])",
        ["leyu_api_base", "leyu_api_token", "leyu_request_id", "telegram_api_id", "telegram_api_hash", "telegram_session",
         "telegram_account", "telegram_source_enabled", "telegram_monitored_chats"],
    )
    decrypt = request.app.state.decrypt_secret
    result: dict[str, str] = {}
    for row in rows:
        try:
            result[row["setting_key"]] = decrypt(row["encrypted_value"])
        except Exception:
            raise HTTPException(503, '采集配置无法解密，请核对配置密钥版本') from None
    return {"items": result}


def _authorized(request: Request, token: str) -> None:
    supplied = request.headers.get("authorization", "")
    if not token or not supplied.lower().startswith("bearer ") or not hmac.compare_digest(supplied[7:].strip(), token):
        raise HTTPException(status_code=401, detail="service authentication failed")



class FixtureQuote(BaseModel):
    market_id: str = Field(min_length=1, max_length=100)
    market_name: str = Field(min_length=1, max_length=160)
    line: Decimal = Decimal("0")
    selection: str = Field(min_length=1, max_length=40)
    price: Decimal | None = Field(default=None, gt=Decimal("1"), max_digits=12, decimal_places=5)
    source_updated_at: AwareDatetime | None = None
    received_at: AwareDatetime
    source_time_quality: str = Field(default="unknown", max_length=40)
    period: str = Field(default="full_time", max_length=40)
    kind: str = Field(default="unsupported", max_length=60)
    status: str = Field(default="active", pattern="^(active|suspended|closed)$")
    play_id: str | None = Field(default=None, max_length=100)
    market_instance_id: str | None = Field(default=None, max_length=100)
    selection_id: str | None = Field(default=None, max_length=100)
    is_main: bool = False
    score_basis: str = Field(default='unknown', pattern='^(unknown|full_period|remaining_period)$')


class FixtureInput(BaseModel):
    id: str = Field(min_length=1, max_length=160)
    source_id: str = Field(min_length=1, max_length=160)
    sport: str = Field(min_length=1, max_length=20)
    competition_id: str | None = None
    competition_name: str = Field(min_length=1, max_length=160)
    home_team_id: str | None = None
    home_team: str = Field(min_length=1, max_length=160)
    home_aliases: list[str] = Field(default_factory=list, max_length=30)
    away_team_id: str | None = None
    away_team: str = Field(min_length=1, max_length=160)
    away_aliases: list[str] = Field(default_factory=list, max_length=30)
    kickoff_at: AwareDatetime
    phase: str = Field(pattern="^(scheduled|live|finished|cancelled)$")
    match_state: str = Field(default="scheduled", max_length=60)
    home_score: int | None = None
    away_score: int | None = None
    clock_minute: int | None = Field(default=None, ge=0, le=200)
    source_updated_at: AwareDatetime | None = None
    received_at: AwareDatetime
    source_time_quality: str = Field(default="unknown", max_length=40)
    half_home_score: int | None = Field(default=None, ge=0)
    half_away_score: int | None = Field(default=None, ge=0)
    markets_complete: bool = False
    markets: list[FixtureQuote] = Field(default_factory=list, max_length=500)


class FixtureBatch(BaseModel):
    items: list[FixtureInput] = Field(min_length=1, max_length=500)



@router.post("/collector/fixtures")
async def ingest_fixtures(body: FixtureBatch, request: Request):
    _authorized(request, request.app.state.settings.collector_token)
    from ..ingestion import ingest_fixture

    accepted = 0
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            for item in body.items:
                accepted += int(await ingest_fixture(conn, item))
    return {"accepted": accepted, "ignored_stale": len(body.items) - accepted}
