from __future__ import annotations

import asyncio
import hmac
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from .. import database

MAX_FIXTURE_BODY_BYTES = 4 * 1024 * 1024


class BoundedCollectorRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            if request.url.path == '/internal/collector/fixtures':
                body = bytearray()
                try:
                    async with asyncio.timeout(10):
                        async for chunk in request.stream():
                            if len(body) + len(chunk) > MAX_FIXTURE_BODY_BYTES:
                                return JSONResponse(status_code=413, content={'detail': 'fixture snapshot body budget exceeded'})
                            body.extend(chunk)
                except TimeoutError:
                    return JSONResponse(status_code=408, content={'detail': 'fixture snapshot body deadline exceeded'})
                request._body = bytes(body)
            return await handler(request)

        return bounded


router = APIRouter(prefix="/internal", route_class=BoundedCollectorRoute)


@router.get("/review/config")
async def review_configuration(request: Request):
    # Internal gateway token only; never return provider credentials or collector settings.
    _authorized(request, getattr(request.app.state.settings, "hermes_api_key", ""))
    from ..runtime_settings import runtime_configuration
    result = await runtime_configuration(request.app.state.settings)
    return {**result, "provider": getattr(request.app.state.settings, "hermes_provider", "custom:cline")}


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
    from ..runtime_settings import runtime_quote_max_age
    result['quote_max_age_seconds'] = str(await runtime_quote_max_age(request.app.state.settings))
    return {"items": result}


def _authorized(request: Request, token: str) -> None:
    supplied = request.headers.get("authorization", "")
    if not token or not supplied.lower().startswith("bearer ") or not hmac.compare_digest(supplied[7:].strip(), token):
        raise HTTPException(status_code=401, detail="service authentication failed")



class FixtureQuote(BaseModel):
    market_id: str = Field(min_length=1, max_length=100)
    market_name: str = Field(min_length=1, max_length=160)
    # Absence is meaningful only for winner markets; a handicap of zero is explicit.
    line: Decimal | None = Field(allow_inf_nan=False, max_digits=8, decimal_places=3)
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

    @model_validator(mode='after')
    def validate_market_line(self):
        if self.kind == 'winner':
            if self.line not in (None, Decimal('0')):
                raise ValueError('winner market has no numeric line')
            self.line = None  # Explicit legacy zero remains accepted at the boundary.
        elif self.line is None:
            raise ValueError('numeric market requires an explicit line; zero is valid')
        elif abs(self.line) > 1000:
            raise ValueError('market line exceeds supported range')
        return self


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
    market_chunks: list[list[FixtureQuote]] | None = Field(default=None, min_length=1, max_length=4)
    market_chunk_count: int | None = Field(default=None, ge=1, le=4, strict=True)
    market_quote_count: int | None = Field(default=None, ge=1, le=2000, strict=True)

    @model_validator(mode='after')
    def validate_market_snapshot(self):
        if self.market_chunks is not None:
            if 'markets' in self.model_fields_set:
                raise ValueError('markets and market_chunks are mutually exclusive')
            if any(not 1 <= len(chunk) <= 500 for chunk in self.market_chunks):
                raise ValueError('each market chunk must contain 1..500 quotes')
            if self.market_chunk_count != len(self.market_chunks):
                raise ValueError('market chunk count does not match complete snapshot')
            if self.market_quote_count != sum(map(len, self.market_chunks)):
                raise ValueError('market quote count does not match complete snapshot')
        elif self.market_chunk_count is not None or self.market_quote_count is not None:
            raise ValueError('market chunk metadata requires market_chunks')
        identities = [(q.market_id, q.period, q.line, q.selection.casefold()) for q in self.all_markets()]
        if len(identities) != len(set(identities)):
            raise ValueError('duplicate quote reference in fixture snapshot')
        return self

    def all_markets(self) -> list[FixtureQuote]:
        return self.markets if self.market_chunks is None else [q for chunk in self.market_chunks for q in chunk]


class FixtureBatch(BaseModel):
    items: list[FixtureInput] = Field(min_length=1, max_length=500)

    @model_validator(mode='after')
    def validate_quote_budget(self):
        if sum(len(item.all_markets()) for item in self.items) > 2000:
            raise ValueError('fixture batch quote budget exceeded')
        return self



@router.post("/collector/fixtures")
async def ingest_fixtures(body: FixtureBatch, request: Request):
    _authorized(request, request.app.state.settings.collector_token)
    from ..ingestion import ingest_fixture_batch

    accepted = 0
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            accepted = await ingest_fixture_batch(conn, body.items)
    return {"accepted": accepted, "ignored_stale": len(body.items) - accepted}
