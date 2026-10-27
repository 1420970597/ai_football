from __future__ import annotations

import base64
import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
import httpx
from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator

from .. import database
from ..review_policy import REALTIME_WINDOW_SECONDS
from ..review_state import authorize, fresh_quote, quote_context
from ..review_tool_evidence import active_tool_job, tool_evidence, tool_success, FIXTURE_FIELDS, MAX_MARKETS
from ..runtime_settings import runtime_quote_max_age

router = APIRouter(prefix="/internal/mcp")
_CANDIDATE_JSON = 'jsonb_build_object(' + ','.join("'" + field + "',f." + field for field in sorted(FIXTURE_FIELDS)) + ')'


class AttemptInput(BaseModel):
    review_id: uuid.UUID
    attempt_id: uuid.UUID


class SearchInput(AttemptInput):
    team_query: str = Field(min_length=2, max_length=120)
    phase: str = Field(default="all", pattern="^(all|live|scheduled)$")
    competition_query: str | None = Field(default=None, max_length=120)
    sport: str | None = Field(default=None, min_length=1, max_length=20, pattern=r'^[a-z][a-z0-9_]{0,19}$')
    from_utc: AwareDatetime | None = None
    to_utc: AwareDatetime | None = None
    cursor: str | None = Field(default=None, max_length=1000)
    page_size: int = Field(default=50, ge=1, le=100)

    @field_validator('sport', mode='before')
    @classmethod
    def normalize_sport(cls, value):
        return value.strip().lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def valid_window(self):
        if self.from_utc and self.to_utc and self.from_utc > self.to_utc:
            raise ValueError("start must precede end")
        if not self.team_query.strip():
            raise ValueError("team name is required")
        return self


class ResolveInput(AttemptInput):
    search_key: str = Field(min_length=64, max_length=64)
    team_a: str = Field(min_length=2, max_length=160)
    team_b: str = Field(min_length=2, max_length=160)
    competition: str | None = Field(default=None, max_length=160)
    from_utc: AwareDatetime | None = None
    to_utc: AwareDatetime | None = None


class MarketInput(AttemptInput):
    fixture_id: str = Field(min_length=1, max_length=160)


class PickInput(BaseModel):
    fixture_id: str = Field(min_length=1, max_length=160)
    quote_id: uuid.UUID
    evidence_text: str = Field(min_length=1, max_length=3000)
    confidence: Decimal = Field(default=Decimal("0"), ge=0, le=1, max_digits=5, decimal_places=4)


class RecordInput(AttemptInput):
    signals: list[PickInput] = Field(min_length=1, max_length=32)


def _key(request: Request) -> bytes:
    return request.app.state.settings.mcp_internal_token.encode()


def _cursor(request: Request, review_id, attempt_id, search_key: str, position: int) -> str:
    body = json.dumps([str(review_id), str(attempt_id), search_key, position], separators=(",", ":")).encode()
    signature = hmac.new(_key(request), body, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(body + signature).decode().rstrip("=")


def _position(request: Request, body: SearchInput, search_key: str) -> int:
    if body.cursor is None:
        return 0
    try:
        raw = base64.b64decode(body.cursor + "=" * (-len(body.cursor) % 4), altchars=b"-_", validate=True)
        encoded, signature = raw[:-32], raw[-32:]
        if not hmac.compare_digest(signature, hmac.new(_key(request), encoded, hashlib.sha256).digest()):
            raise ValueError
        review_id, attempt_id, key, position = json.loads(encoded)
        if (review_id, attempt_id, key) != (str(body.review_id), str(body.attempt_id), search_key) or not isinstance(position, int) or position < 1:
            raise ValueError
        return position
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(status_code=400, detail="invalid or cross-query cursor") from None


@router.post("/search_matches")
@tool_evidence('mcp_search_matches')
async def search_matches(body: SearchInput, request: Request):
    authorize(request, request.app.state.settings.mcp_internal_token)
    scope = {"team": body.team_query.strip().casefold(), "phase": body.phase, "competition": (body.competition_query or "").strip().casefold(), "sport": body.sport,
             "from": body.from_utc.astimezone(timezone.utc).isoformat() if body.from_utc else None,
             "to": body.to_utc.astimezone(timezone.utc).isoformat() if body.to_utc else None}
    search_key = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            await active_tool_job(conn, body.review_id, body.attempt_id)
            search = await conn.fetchrow("SELECT * FROM v2_match_searches WHERE job_id=$1 AND search_key=$2 FOR UPDATE", body.review_id, search_key)
            if not search:
                if body.cursor:
                    raise HTTPException(status_code=409, detail="search has not started")
                await conn.execute(
                    "INSERT INTO v2_match_searches(job_id,search_key,team_query,phase,competition_query,from_utc,to_utc,attempt_id) "
                    "VALUES($1,$2,$3,$4,$5,$6,$7,$8)", body.review_id, search_key, scope["team"], body.phase, scope["competition"], body.from_utc, body.to_utc, body.attempt_id,
                )
                # 一次 INSERT SELECT 形成完整候选快照，之后按不可变序号遍历，避免更新和新增造成漏场。
                await conn.execute(
                    "INSERT INTO v2_match_search_candidates(job_id,search_key,fixture_id,position,candidate_data) "
                    "SELECT $1,$2,f.id,row_number() OVER(ORDER BY f.kickoff_at,f.id)," + _CANDIDATE_JSON + " FROM v2_fixtures f "
                    "WHERE f.phase IN ('live','scheduled') AND ($3='all' OR f.phase=$3) "
                    "AND (lower(f.home_team) LIKE $4 OR lower(f.away_team) LIKE $4 OR $5=ANY(f.home_aliases) OR $5=ANY(f.away_aliases)) "
                    "AND ($6='' OR lower(f.competition_name) LIKE '%'||$6||'%') "
                    "AND ($7::timestamptz IS NULL OR f.kickoff_at >= $7) AND ($8::timestamptz IS NULL OR f.kickoff_at <= $8) "
                    "AND ($9::text IS NULL OR f.sport=$9)",
                    body.review_id, search_key, body.phase, "%" + scope["team"] + "%", scope["team"], scope["competition"], body.from_utc, body.to_utc, body.sport,
                )
                search = await conn.fetchrow("SELECT * FROM v2_match_searches WHERE job_id=$1 AND search_key=$2", body.review_id, search_key)
            if search["attempt_id"] != body.attempt_id:
                raise HTTPException(status_code=409, detail="search belongs to an earlier attempt")
            if body.cursor != search["expected_cursor"] and not (body.cursor is None and search["page_count"] == 0):
                raise HTTPException(status_code=409, detail="cursor is not the next page")
            if search["complete"]:
                raise HTTPException(status_code=409, detail="search already complete; use resolution tool")
            position = _position(request, body, search_key)
            rows = await conn.fetch(
                "SELECT position,candidate_data FROM v2_match_search_candidates WHERE job_id=$1 AND search_key=$2 "
                "AND position>$3 ORDER BY position LIMIT $4", body.review_id, search_key, position, body.page_size + 1,
            )
            has_more = len(rows) > body.page_size
            page = rows[:body.page_size]
            cursor = _cursor(request, body.review_id, body.attempt_id, search_key, page[-1]["position"]) if has_more else None
            total = await conn.fetchval("SELECT count(*) FROM v2_match_search_candidates WHERE job_id=$1 AND search_key=$2", body.review_id, search_key)
            await conn.execute(
                "UPDATE v2_match_searches SET expected_cursor=$3,complete=$4,page_count=page_count+1,total_candidates=$5,updated_at=now() WHERE job_id=$1 AND search_key=$2",
                body.review_id, search_key, cursor, not has_more, total,
            )
            result = {"search_key": search_key, "items": [row["candidate_data"] for row in page], "next_cursor": cursor, "search_complete": not has_more, "total_candidates": total}
            return await tool_success(conn, body, result, {"scope": scope, "search_key": search_key, "after": position, "returned": len(page), "total": total, "complete": not has_more})


@router.post("/resolve_match")
@tool_evidence('mcp_resolve_match')
async def resolve_match(body: ResolveInput, request: Request):
    authorize(request, request.app.state.settings.mcp_internal_token)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            await active_tool_job(conn, body.review_id, body.attempt_id)
            complete = await conn.fetchval("SELECT complete FROM v2_match_searches WHERE job_id=$1 AND search_key=$2 AND attempt_id=$3", body.review_id, body.search_key, body.attempt_id)
            if not complete:
                raise HTTPException(status_code=409, detail="all candidate pages must be read first")
            rows = await conn.fetch("SELECT fixture_id,candidate_data FROM v2_match_search_candidates WHERE job_id=$1 AND search_key=$2", body.review_id, body.search_key)
            matches = []
            a, b = body.team_a.strip().casefold(), body.team_b.strip().casefold()
            if a == b:
                raise HTTPException(status_code=422, detail="two distinct team identities are required")
            for row in rows:
                fixture = row["candidate_data"]
                home = {str(v).casefold() for v in [fixture["home_team"], *fixture.get("home_aliases", [])]}
                away = {str(v).casefold() for v in [fixture["away_team"], *fixture.get("away_aliases", [])]}
                if not ((a in home and b in away) or (b in home and a in away)):
                    continue
                if body.competition and body.competition.strip().casefold() != fixture["competition_name"].casefold():
                    continue
                kickoff = datetime.fromisoformat(fixture["kickoff_at"])
                if body.from_utc and kickoff < body.from_utc or body.to_utc and kickoff > body.to_utc:
                    continue
                matches.append(row)
            if len(matches) != 1:
                result = {"resolved": False, "reason": "no_unique_fixture", "candidate_count": len(matches)}
            else:
                fixture = matches[0]
                evidence = jsonable_encoder(body.model_dump(exclude={"review_id", "attempt_id"}))
                await conn.execute(
                    "INSERT INTO v2_review_fixture_bindings(job_id,attempt_id,fixture_id,search_key,evidence) VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING",
                    body.review_id, body.attempt_id, fixture["fixture_id"], body.search_key, evidence,
                )
                result = {"resolved": True, "fixture": fixture["candidate_data"]}
            return await tool_success(conn, body, result, {'result': {'resolved': result['resolved'], 'candidate_count': result.get('candidate_count')}})


async def _refresh_bound_market(body: MarketInput, request: Request) -> dict:
    """Verify ownership first; hold no database connection across source I/O."""
    settings = request.app.state.settings
    url = getattr(settings, 'market_refresh_url', '')
    if not url:
        return {'attempted': False, 'reason': 'refresh_unconfigured'}
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            job = await active_tool_job(conn, body.review_id, body.attempt_id)
            age = (datetime.now(timezone.utc) - job['posted_at']).total_seconds()
            if job['review_mode'] != 'real_time' or not 0 <= age <= REALTIME_WINDOW_SECONDS:
                return {'attempted': False, 'reason': 'historical_review_not_current'}
            bound = await conn.fetchval(
                'SELECT EXISTS(SELECT 1 FROM v2_review_fixture_bindings WHERE job_id=$1 AND attempt_id=$2 AND fixture_id=$3)',
                body.review_id, body.attempt_id, body.fixture_id)
            if not bound:
                raise HTTPException(409, detail='fixture must be uniquely resolved first')
            fixture = await conn.fetchrow(
                "SELECT source_id AS source_match_id FROM v2_fixtures WHERE id=$1 AND phase IN ('scheduled','live')", body.fixture_id)
            if not fixture:
                raise HTTPException(409, detail='fixture is no longer active')
            quotes = await conn.fetch('SELECT * FROM v2_current_quotes WHERE fixture_id=$1 LIMIT $2', body.fixture_id, MAX_MARKETS + 1)
            if len(quotes) > MAX_MARKETS:
                raise HTTPException(413, detail='tool evidence budget exceeded; no partial response returned')
            max_age = await runtime_quote_max_age(settings, conn)
            if quotes and all(fresh_quote(quote, max_age) for quote in quotes):
                return {'attempted': False, 'reason': 'quotes_already_fresh'}
            source_match_id = fixture['source_match_id']
    try:
        async with asyncio.timeout(9):
            async with httpx.AsyncClient(timeout=httpx.Timeout(8.5, connect=2), trust_env=False, follow_redirects=False) as client:
                response = await client.post(url, headers={'Authorization': 'Bearer ' + settings.collector_token},
                    json={'fixture_id': body.fixture_id, 'source_match_id': source_match_id})
                response.raise_for_status()
                value = response.json()
        if not isinstance(value, dict) or type(value.get('accepted')) is not bool:
            return {'attempted': True, 'accepted': False, 'reason': 'refresh_invalid_response'}
        reason = str(value.get('reason', ''))
        if not reason or not all(char.isascii() and (char.isalnum() or char == '_') for char in reason) or len(reason) > 80:
            reason = 'refresh_completed' if value['accepted'] else 'refresh_unavailable'
        return {'attempted': True, 'accepted': value['accepted'], 'reason': reason}
    except (httpx.HTTPError, ValueError, TimeoutError):
        # A refresh is optional evidence gathering, never a reason to mark old
        # prices fresh or replay an unknown upstream operation automatically.
        return {'attempted': True, 'accepted': False, 'reason': 'refresh_unavailable'}


@router.post("/list_match_markets")
@tool_evidence('mcp_list_match_markets')
async def list_match_markets(body: MarketInput, request: Request):
    authorize(request, request.app.state.settings.mcp_internal_token)
    refresh = await _refresh_bound_market(body, request)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            await active_tool_job(conn, body.review_id, body.attempt_id)
            bound = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM v2_review_fixture_bindings WHERE job_id=$1 AND attempt_id=$2 AND fixture_id=$3)", body.review_id, body.attempt_id, body.fixture_id)
            if not bound:
                raise HTTPException(status_code=409, detail="fixture must be uniquely resolved first")
            fixture = await conn.fetchrow("SELECT * FROM v2_fixtures WHERE id=$1 AND phase IN ('scheduled','live')", body.fixture_id)
            if not fixture:
                raise HTTPException(status_code=409, detail="fixture is no longer active")
            quotes = await conn.fetch("SELECT * FROM v2_current_quotes WHERE fixture_id=$1 ORDER BY period,market_id,line,selection LIMIT $2", body.fixture_id, MAX_MARKETS + 1)
            if len(quotes) > MAX_MARKETS:
                raise HTTPException(413, detail='tool evidence budget exceeded; no partial response returned')
            if quotes:
                await conn.executemany(
                    "INSERT INTO v2_review_market_reads(job_id,attempt_id,quote_id,source_updated_at,price,quote_context) VALUES($1,$2,$3,$4,$5,$6) "
                    "ON CONFLICT(job_id,attempt_id,quote_id) DO UPDATE SET source_updated_at=EXCLUDED.source_updated_at,price=EXCLUDED.price,quote_context=EXCLUDED.quote_context,created_at=now()",
                    [(body.review_id, body.attempt_id, q["id"], q["source_updated_at"], q["price"], quote_context(q, fixture)) for q in quotes],
                )
            max_quote_age = await runtime_quote_max_age(request.app.state.settings, conn)
            result = {"fixture": dict(fixture), "markets": [dict(q) | {"quote_id": str(q["id"]), "fresh": fresh_quote(q, max_quote_age)} for q in quotes],
                      "refresh": refresh}
            return await tool_success(conn, body, result, {"fixture_id": body.fixture_id, "quote_ids": [str(q["id"]) for q in quotes], "refresh": refresh})


@router.post("/record_expert_signals")
@tool_evidence('mcp_record_expert_signals')
async def record_expert_signals(body: RecordInput, request: Request):
    authorize(request, request.app.state.settings.mcp_internal_token)
    accepted, rejected = [], []
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            job = await active_tool_job(conn, body.review_id, body.attempt_id)
            message_age = (datetime.now(timezone.utc) - job['posted_at']).total_seconds()
            current_message = 0 <= message_age <= REALTIME_WINDOW_SECONDS
            if job['review_mode'] == 'historical_replay' or not current_message:
                reason = ('message_timestamp_in_future' if message_age < 0
                          else 'historical_review_not_current')
                result = {"expert": job["display_nickname"], "accepted": [],
                          "rejected": [{"index": index, "reason": reason}
                                       for index, _ in enumerate(body.signals)]}
                return await tool_success(conn, body, result, {"review_mode": job['review_mode'], **result,
                    'message_age_seconds': round(message_age, 3)})
            max_quote_age = await runtime_quote_max_age(request.app.state.settings, conn)
            for index, pick in enumerate(body.signals):
                quote = await conn.fetchrow(
                    "SELECT q.*,f.phase,f.home_score,f.away_score,f.half_home_score,f.half_away_score,r.quote_context AS read_context FROM v2_current_quotes q "
                    "JOIN v2_fixtures f ON f.id=q.fixture_id JOIN v2_review_fixture_bindings b ON b.fixture_id=q.fixture_id AND b.job_id=$1 AND b.attempt_id=$2 "
                    "JOIN v2_review_market_reads r ON r.quote_id=q.id AND r.job_id=$1 AND r.attempt_id=$2 "
                    "WHERE q.id=$3 AND q.fixture_id=$4 AND q.price IS NOT DISTINCT FROM r.price AND q.source_updated_at IS NOT DISTINCT FROM r.source_updated_at FOR SHARE OF q,f",
                    body.review_id, body.attempt_id, pick.quote_id, pick.fixture_id,
                )
                reason = None
                if not quote:
                    reason = "quote_not_read_or_changed"
                elif quote['read_context'] != quote_context(quote, quote):
                    reason = 'quote_or_score_context_changed'
                elif quote["phase"] not in {"scheduled", "live"}:
                    reason = "fixture_not_active"
                elif not fresh_quote(quote, max_quote_age):
                    reason = "quote_inactive_unknown_or_stale"
                if reason:
                    rejected.append({"index": index, "reason": reason})
                    continue
                baseline = {"home": quote["home_score"], "away": quote["away_score"], "half_home": quote["half_home_score"], "half_away": quote["half_away_score"]}
                new_id = uuid.uuid4()
                result = await conn.fetchrow(
                    "INSERT INTO v2_expert_signals(id,message_id,expert_id,fixture_id,phase,market_id,market_name,period,market_kind,line,selection,recommendation_price,quote_time,confidence,evidence_text,score_baseline,score_basis) "
                    "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17) ON CONFLICT(message_id,fixture_id,market_id,period,(COALESCE(line,0)),selection) DO NOTHING RETURNING id",
                    new_id, job["message_id"], job["expert_id"], quote["fixture_id"], quote["phase"], quote["market_id"], quote["market_name"], quote["period"], quote["market_kind"], quote["line"], quote["selection"], quote["price"], quote["source_updated_at"], pick.confidence, pick.evidence_text, baseline, quote["score_basis"],
                )
                if result:
                    await conn.execute(
                        "INSERT INTO v2_quote_evidence(id,fixture_id,market_id,market_name,period,line,selection,price,evidence_kind,reference_id,source_updated_at,evidence,score_baseline) VALUES($1,$2,$3,$4,$5,$6,$7,$8,'expert_signal',$9,$10,$11,$12)",
                        uuid.uuid4(), quote["fixture_id"], quote["market_id"], quote["market_name"], quote["period"], quote["line"], quote["selection"], quote["price"], new_id, quote["source_updated_at"], {"job_id": str(body.review_id), "attempt_id": str(body.attempt_id), "quote_id": str(pick.quote_id), "score_basis": quote["score_basis"], "market_kind": quote["market_kind"]}, baseline,
                    )
                else:
                    new_id = await conn.fetchval("SELECT id FROM v2_expert_signals WHERE message_id=$1 AND fixture_id=$2 AND market_id=$3 AND period=$4 AND selection=$6 AND market_kind=$7 AND (line IS NOT DISTINCT FROM $5::numeric OR market_kind='winner' AND COALESCE(line,0)=0 AND COALESCE($5::numeric,0)=0)", job["message_id"], quote["fixture_id"], quote["market_id"], quote["period"], quote["line"], quote["selection"], quote["market_kind"])
                accepted.append({"index": index, "signal_id": str(new_id), "duplicate": not bool(result), "fixture_id": quote["fixture_id"], "market_name": quote["market_name"], "period": quote["period"], "line": str(quote["line"]) if quote["line"] is not None else None, "selection": quote["selection"], "price": str(quote["price"])})
            result = {"expert": job["display_nickname"], "accepted": accepted, "rejected": rejected}
            return await tool_success(conn, body, result, result)
