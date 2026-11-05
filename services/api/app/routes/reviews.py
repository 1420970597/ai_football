from __future__ import annotations

import asyncio
import hashlib
import json
import unicodedata
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
import httpx
from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator

from .. import database
from ..review_policy import REALTIME_WINDOW_SECONDS
from ..handicap_lines import line_terms
from ..review_state import authorize, fresh_quote, quote_context
from ..review_tool_evidence import active_tool_job, tool_evidence, tool_success, MAX_MARKETS
from ..runtime_settings import runtime_quote_max_age
from ..quote_observation import confirmation_for
from ..team_matching import normalize_team_name, team_name_matches
from ..review_candidates import snapshot_candidates
from ..review_cursors import encode_cursor, decode_cursor
from ..review_search_scope import continuation_scope, scope_key

router = APIRouter(prefix="/internal/mcp")

# Reserve one database command budget before reusing a precise observation.
# The final write still validates its actual age against the unchanged policy.
QUOTE_CONFIRMATION_WRITE_HEADROOM_SECONDS = 5


class AttemptInput(BaseModel):
    review_id: uuid.UUID
    attempt_id: uuid.UUID


class SearchInput(AttemptInput):
    team_query: str = Field(min_length=2, max_length=120)
    phase: str = Field(default="all", pattern="^(all|live|scheduled|finished|cancelled)$")
    competition_query: str | None = Field(default=None, max_length=120)
    sport: str | None = Field(default=None, min_length=1, max_length=20, pattern=r'^[a-z][a-z0-9_]{0,19}$')
    from_utc: AwareDatetime | None = None
    to_utc: AwareDatetime | None = None
    cursor: str | None = Field(default=None, max_length=1000)
    page_size: int = Field(default=100, ge=1, le=100)

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
    market_kind: str | None = Field(default=None, max_length=32, pattern=r'^[a-z][a-z0-9_]{0,31}$')
    period: str | None = Field(default=None, max_length=32, pattern=r'^[a-z][a-z0-9_]{0,31}$')
    line: Decimal | None = Field(default=None, max_digits=8, decimal_places=3)
    selection: str | None = Field(default=None, max_length=32, pattern=r'^[a-z][a-z0-9_]{0,31}$')
    market_query: str | None = Field(default=None, max_length=120)

    @field_validator('market_kind', mode='before')
    @classmethod
    def normalize_market_kind_alias(cls, value):
        if value == 'over_under':
            return 'total'
        return 'handicap' if value == 'asian_handicap' else value

    @field_validator('period', mode='before')
    @classmethod
    def normalize_full_period_alias(cls, value):
        return 'full_time' if value == 'full_match' else value

    @field_validator('market_query', mode='before')
    @classmethod
    def normalize_market_query(cls, value):
        if not isinstance(value, str):
            return value
        normalized = ' '.join(unicodedata.normalize('NFKC', value).casefold().split())
        return normalized or None


class PickInput(BaseModel):
    fixture_id: str = Field(min_length=1, max_length=160)
    quote_id: uuid.UUID
    evidence_text: str = Field(min_length=1, max_length=3000)
    confidence: Decimal = Field(default=Decimal("0"), ge=0, le=1, max_digits=5, decimal_places=4)


class RecordInput(AttemptInput):
    signals: list[PickInput] = Field(min_length=1, max_length=32)


def _market_matches(quote, filters):
    for field in ('market_kind', 'period', 'selection'):
        expected = filters[field]
        if expected is not None and quote.get(field) != expected:
            return False
    line = filters['line']
    if line is not None:
        actual = quote.get('line')
        if actual is None or Decimal(str(actual)) != line:
            return False
    query = filters['market_query']
    if query is not None:
        name = unicodedata.normalize('NFKC', str(quote.get('market_name') or '')).casefold()
        kind = str(quote.get('market_kind') or '').casefold()
        if query not in f'{name} {kind}':
            return False
    return True


def _market_filter_sql(fixture_id: str, filters: dict):
    clauses = ['q.fixture_id=$1']
    values = [fixture_id]
    for field in ('market_kind', 'period', 'selection'):
        value = filters[field]
        if value is not None:
            values.append(value)
            clauses.append(f'q.{field}=${len(values)}')
    if filters['line'] is not None:
        values.append(filters['line'])
        clauses.append(f'q.line=${len(values)}::numeric')
    return ' AND '.join(clauses), values


async def _read_market_quotes(conn, fixture_id: str, filters: dict):
    where, values = _market_filter_sql(fixture_id, filters)
    values.append(MAX_MARKETS + 1)
    rows = await conn.fetch(
        'SELECT inventory.total_quotes,q.* '
        'FROM (SELECT count(*)::integer AS total_quotes FROM v2_current_quotes WHERE fixture_id=$1) inventory '
        'LEFT JOIN LATERAL (SELECT q.* FROM v2_current_quotes q WHERE ' + where +
        ' ORDER BY q.period,q.market_id,q.line,q.selection LIMIT $' + str(len(values)) + ') q ON TRUE',
        *values,
    )
    quotes = [row for row in rows if row['id'] is not None]
    return rows[0]['total_quotes'], quotes


def _key(request: Request) -> bytes:
    return request.app.state.settings.mcp_internal_token.encode()


def _cursor(request: Request, review_id, attempt_id, search_key: str, position: int) -> str:
    return encode_cursor(_key(request), review_id, attempt_id, search_key, position)


def _position(request: Request, body: SearchInput, search_key: str) -> int:
    if body.cursor is None:
        return 0
    try:
        return decode_cursor(_key(request), body.review_id, body.attempt_id, search_key, body.cursor)
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(status_code=400, detail="invalid or cross-query cursor") from None


@router.post("/search_matches")
@tool_evidence('mcp_search_matches')
async def search_matches(body: SearchInput, request: Request):
    authorize(request, request.app.state.settings.mcp_internal_token)
    scope = {"team": body.team_query.strip().casefold(), "phase": body.phase, "competition": (body.competition_query or "").strip().casefold(), "sport": body.sport,
             "from": body.from_utc.astimezone(timezone.utc).isoformat() if body.from_utc else None,
             "to": body.to_utc.astimezone(timezone.utc).isoformat() if body.to_utc else None}
    search_key = scope_key(scope)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            await active_tool_job(conn, body.review_id, body.attempt_id)
            search = None
            candidate_total = None
            if body.cursor:
                # Lock the actual continuation first; never acquire two query
                # locks in opposite orders when a caller changes its filters.
                search = await conn.fetchrow(
                    "SELECT * FROM v2_match_searches WHERE job_id=$1 AND attempt_id=$2 "
                    "AND expected_cursor=$3 FOR UPDATE", body.review_id, body.attempt_id, body.cursor)
                if search and search['search_key'] != search_key:
                    recorded = await conn.fetchval(
                        "SELECT payload->'scope' FROM v2_analysis_traces WHERE job_id=$1 "
                        "AND trace_kind='mcp_search_matches' AND payload->>'status'='succeeded' "
                        "AND payload->>'attempt_id'=$2 AND payload->>'search_key'=$3 "
                        "AND payload ? 'scope' ORDER BY sequence_no DESC LIMIT 1",
                        body.review_id, str(body.attempt_id), search['search_key'])
                    try:
                        scope = continuation_scope(scope, recorded, search['search_key'])
                    except ValueError as exc:
                        raise HTTPException(409, detail=str(exc)) from None
                    search_key = search['search_key']
            if search is None:
                search = await conn.fetchrow("SELECT * FROM v2_match_searches WHERE job_id=$1 AND search_key=$2 FOR UPDATE", body.review_id, search_key)
            if not search:
                if body.cursor:
                    raise HTTPException(status_code=409, detail="search has not started")
                search = await conn.fetchrow(
                    "INSERT INTO v2_match_searches(job_id,search_key,team_query,phase,competition_query,from_utc,to_utc,attempt_id) "
                    "VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING *", body.review_id, search_key, scope["team"], body.phase, scope["competition"], body.from_utc, body.to_utc, body.attempt_id,
                )
                # 一次 INSERT SELECT 形成完整候选快照，之后按不可变序号遍历，避免更新和新增造成漏场。
                candidate_total = await snapshot_candidates(
                    conn, review_id=body.review_id, search_key=search_key, phase=body.phase,
                    team_query=body.team_query, competition=scope['competition'], sport=body.sport,
                    from_utc=body.from_utc, to_utc=body.to_utc,
                )
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
            total = candidate_total if candidate_total is not None else search["total_candidates"]
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
            if normalize_team_name(body.team_a) == normalize_team_name(body.team_b):
                raise HTTPException(status_code=422, detail="two distinct team identities are required")
            for row in rows:
                fixture = row["candidate_data"]
                home_match = team_name_matches(body.team_a, fixture["home_team"], fixture.get("home_aliases", []))
                away_match = team_name_matches(body.team_a, fixture["away_team"], fixture.get("away_aliases", []))
                home_other = team_name_matches(body.team_b, fixture["home_team"], fixture.get("home_aliases", []))
                away_other = team_name_matches(body.team_b, fixture["away_team"], fixture.get("away_aliases", []))
                if not ((home_match and away_other) or (away_match and home_other)):
                    continue
                if body.competition and normalize_team_name(body.competition) != normalize_team_name(fixture["competition_name"]):
                    continue
                kickoff = datetime.fromisoformat(fixture["kickoff_at"])
                if body.from_utc and kickoff < body.from_utc or body.to_utc and kickoff > body.to_utc:
                    continue
                matches.append(row)
            if len(matches) != 1:
                result = {"resolved": False, "reason": "no_matching_fixture" if not matches else "ambiguous_fixture", "candidate_count": len(matches)}
            else:
                fixture = matches[0]
                evidence = jsonable_encoder(body.model_dump(exclude={"review_id", "attempt_id"}))
                await conn.execute(
                    "INSERT INTO v2_review_fixture_bindings(job_id,attempt_id,fixture_id,search_key,evidence) VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING",
                    body.review_id, body.attempt_id, fixture["fixture_id"], body.search_key, evidence,
                )
                current_phase = await conn.fetchval('SELECT phase FROM v2_fixtures WHERE id=$1', fixture['fixture_id'])
                result = {"resolved": True, "fixture": {**fixture["candidate_data"], 'phase': current_phase,
                    'realtime_eligible': current_phase in {'scheduled', 'live'},
                    'eligibility_reason': None if current_phase in {'scheduled', 'live'} else current_phase}}
            return await tool_success(conn, body, result, {'result': {'resolved': result['resolved'], 'candidate_count': result.get('candidate_count')}})


async def _bound_active_fixture(conn, body: MarketInput) -> dict:
    row = await conn.fetchrow(
        "SELECT f.*,EXISTS(SELECT 1 FROM v2_review_fixture_bindings b "
        "WHERE b.job_id=$1 AND b.attempt_id=$2 AND b.fixture_id=$3) AS review_fixture_bound "
        "FROM (VALUES(1)) AS anchor(value) LEFT JOIN v2_fixtures f "
        "ON f.id=$3 AND f.phase IN ('scheduled','live')",
        body.review_id, body.attempt_id, body.fixture_id,
    )
    fixture = dict(row)
    if not fixture.pop('review_fixture_bound'):
        raise HTTPException(409, detail='fixture must be uniquely resolved first')
    if fixture['id'] is None:
        raise HTTPException(409, detail='fixture is no longer active')
    return fixture


async def _refresh_bound_market(body: MarketInput, request: Request, *, force: bool = False) -> dict:
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
            fixture = await _bound_active_fixture(conn, body)
            filters = {'market_kind': body.market_kind, 'period': body.period, 'line': body.line,
                       'selection': body.selection, 'market_query': body.market_query}
            if not force:
                total_quotes, candidates = await _read_market_quotes(conn, body.fixture_id, filters)
                if len(candidates) > MAX_MARKETS:
                    raise HTTPException(413, detail='tool evidence budget exceeded; no partial response returned')
                quotes = [quote for quote in candidates if _market_matches(quote, filters)]
                if quotes:
                    max_age = await runtime_quote_max_age(settings, conn)
                    if all(fresh_quote(quote, max_age) for quote in quotes):
                        return {'attempted': False, 'reason': 'quotes_already_fresh'}
                elif total_quotes and any(value is not None for value in filters.values()):
                    return {'attempted': False, 'reason': 'no_quotes_for_filter'}
            source_match_id = fixture['source_id']
    return await _request_market_refresh(body.fixture_id, source_match_id, request)


async def _request_market_refresh(fixture_id: str, source_match_id: str, request: Request) -> dict:
    """I/O only, after this call's transaction validated the bound active fixture.

    Callers must revalidate the attempt and quote after I/O before recording.
    Never retain a connection, permission witness or observation across calls.
    """
    settings = request.app.state.settings
    url = getattr(settings, 'market_refresh_url', '')
    if not url:
        return {'attempted': False, 'reason': 'refresh_unconfigured'}
    try:
        async with asyncio.timeout(9):
            async with httpx.AsyncClient(timeout=httpx.Timeout(8.5, connect=2), trust_env=False, follow_redirects=False) as client:
                response = await client.post(url, headers={'Authorization': 'Bearer ' + settings.collector_token},
                    json={'fixture_id': fixture_id, 'source_match_id': source_match_id})
                response.raise_for_status()
                value = response.json()
        if not isinstance(value, dict) or type(value.get('accepted')) is not bool:
            return {'attempted': True, 'accepted': False, 'reason': 'refresh_invalid_response'}
        reason = str(value.get('reason', ''))
        if not reason or not all(char.isascii() and (char.isalnum() or char == '_') for char in reason) or len(reason) > 80:
            reason = 'refresh_completed' if value['accepted'] else 'refresh_unavailable'
        return {'attempted': True, 'accepted': value['accepted'], 'reason': reason,
                '_observation': value.get('current_observation') if value.get('snapshot_committed') is True else None}
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
            fixture = await _bound_active_fixture(conn, body)
            filters = {'market_kind': body.market_kind, 'period': body.period, 'line': body.line,
                       'selection': body.selection, 'market_query': body.market_query}
            total_quotes, candidates = await _read_market_quotes(conn, body.fixture_id, filters)
            if len(candidates) > MAX_MARKETS:
                raise HTTPException(413, detail='tool evidence budget exceeded; no partial response returned')
            quotes = [quote for quote in candidates if _market_matches(quote, filters)]
            max_quote_age = await runtime_quote_max_age(request.app.state.settings, conn)
            confirmations = {q['id']: confirmation_for(refresh.get('_observation'), q, fixture, max_quote_age) for q in quotes}
            if quotes:
                await conn.executemany(
                    "INSERT INTO v2_review_market_reads(job_id,attempt_id,quote_id,source_updated_at,price,quote_context) VALUES($1,$2,$3,$4,$5,$6) "
                    "ON CONFLICT(job_id,attempt_id,quote_id) DO UPDATE SET source_updated_at=EXCLUDED.source_updated_at,price=EXCLUDED.price,quote_context=EXCLUDED.quote_context,created_at=now()",
                    [(body.review_id, body.attempt_id, q["id"], q["source_updated_at"], q["price"],
                      quote_context(q, fixture) | ({'current_confirmation': confirmations[q['id']]} if confirmations[q['id']] else {})) for q in quotes],
                )
            refresh.pop('_observation', None)
            market_filter = {key: str(value) if key == 'line' and value is not None else value
                             for key, value in filters.items() if value is not None}
            market_coverage = {'total_quotes': total_quotes, 'matched_quotes': len(quotes),
                               'returned_quotes': len(quotes), 'complete_for_filter': True}
            result = {"fixture": dict(fixture), "markets": [dict(q) | {"quote_id": str(q["id"]), "fresh": fresh_quote(q, max_quote_age),
                      'confirmed_current': bool(confirmations[q['id']]),
                      'can_record': fresh_quote(q, max_quote_age) or bool(confirmations[q['id']])} for q in quotes],
                      "refresh": refresh, "market_filter": market_filter, "market_coverage": market_coverage}
            return await tool_success(conn, body, result, {"fixture_id": body.fixture_id,
                "quote_ids": [str(q["id"]) for q in quotes], "refresh": refresh,
                "market_filter": market_filter, "market_coverage": market_coverage})


@router.post("/record_expert_signals")
@tool_evidence('mcp_record_expert_signals')
async def record_expert_signals(body: RecordInput, request: Request):
    authorize(request, request.app.state.settings.mcp_internal_token)
    accepted, rejected = [], []
    observations = {}
    # A long model turn must not turn a previously valid read into a permanent
    # rejection. Recheck only the selected, already-read and bound fixtures.
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            job = await active_tool_job(conn, body.review_id, body.attempt_id)
            if job['review_mode'] == 'real_time' and 0 <= (datetime.now(timezone.utc)-job['posted_at']).total_seconds() <= REALTIME_WINDOW_SECONDS:
                rows = await conn.fetch(
                    'SELECT q.*,f.id AS fixture_identity,f.source_id,f.phase,f.home_score,f.away_score,f.half_home_score,f.half_away_score,r.quote_context AS read_context '
                    'FROM v2_current_quotes q JOIN v2_fixtures f ON f.id=q.fixture_id '
                    'JOIN v2_review_market_reads r ON r.quote_id=q.id AND r.job_id=$1 AND r.attempt_id=$2 '
                    'JOIN v2_review_fixture_bindings b ON b.fixture_id=q.fixture_id AND b.job_id=$1 AND b.attempt_id=$2 '
                    'JOIN (SELECT DISTINCT fixture_id,quote_id FROM unnest($3::text[],$4::uuid[]) AS pairs(fixture_id,quote_id)) wanted '
                    'ON wanted.fixture_id=q.fixture_id AND wanted.quote_id=q.id',
                    body.review_id, body.attempt_id,
                    [p.fixture_id for p in body.signals], [p.quote_id for p in body.signals])
                max_age = await runtime_quote_max_age(request.app.state.settings, conn)
                reusable_age = max(0, max_age-QUOTE_CONFIRMATION_WRITE_HEADROOM_SECONDS)
                stale_fixtures = {q['fixture_id']: q['source_id'] for q in rows if q['phase'] in {'scheduled', 'live'} and q['status'] == 'active' and not fresh_quote(q, reusable_age) and not confirmation_for(
                    q['read_context'].get('current_confirmation'), q, dict(q) | {'id': q['fixture_identity']}, reusable_age)}
            else:
                stale_fixtures = {}
    # Shared collector budget still coalesces and limits provider calls. Do not
    # hold the database transaction during I/O or extend a terminated lease.
    for fixture_id in sorted(stale_fixtures):
        if len(observations) >= 4:
            break
        # The preceding transaction already checked this exact requested pair,
        # its read receipt, current binding and active phase. Repeating that
        # transaction before the same I/O adds WAN round trips without making
        # the final post-I/O validation any stronger.
        refreshed = await _request_market_refresh(fixture_id, stale_fixtures[fixture_id], request)
        observations[fixture_id] = refreshed.get('_observation')
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
            # Projection/result ingestion locks fixtures before updating quotes.
            # Lock every selected bound fixture in that same global order before
            # touching a quote; a joined q,f lock can acquire q first and deadlock.
            await conn.fetch(
                'SELECT f.id FROM v2_fixtures f JOIN v2_review_fixture_bindings b ON b.fixture_id=f.id '
                'WHERE b.job_id=$1 AND b.attempt_id=$2 AND f.id=ANY($3::text[]) '
                'ORDER BY f.source_id FOR SHARE OF f',
                body.review_id, body.attempt_id, sorted({pick.fixture_id for pick in body.signals}),
            )
            # Read only requested (fixture, quote) pairs, once in stable order.
            # Keep fixture-before-quote locking while avoiding N remote reads
            # under the fixture locks. Each intent is still validated below.
            quotes = await conn.fetch(
                    "SELECT q.*,f.source_id,f.phase,f.home_score,f.away_score,f.half_home_score,f.half_away_score,r.quote_context AS read_context,r.source_updated_at AS read_source_time,r.price AS read_price FROM v2_current_quotes q "
                    "JOIN v2_fixtures f ON f.id=q.fixture_id JOIN v2_review_fixture_bindings b ON b.fixture_id=q.fixture_id AND b.job_id=$1 AND b.attempt_id=$2 "
                    "JOIN v2_review_market_reads r ON r.quote_id=q.id AND r.job_id=$1 AND r.attempt_id=$2 "
                    "JOIN (SELECT DISTINCT fixture_id,quote_id FROM unnest($3::text[],$4::uuid[]) AS pairs(fixture_id,quote_id)) wanted "
                    "ON wanted.fixture_id=q.fixture_id AND wanted.quote_id=q.id "
                    "ORDER BY q.fixture_id,q.id FOR SHARE OF q",
                    body.review_id, body.attempt_id,
                    [pick.fixture_id for pick in body.signals], [pick.quote_id for pick in body.signals],
            )
            by_quote = {(quote['fixture_id'], quote['id']): quote for quote in quotes}
            for index, pick in enumerate(body.signals):
                # Bind intent and score context, then snapshot current price.
                quote = by_quote.get((pick.fixture_id, pick.quote_id))
                reason = None
                confirmation = None
                if quote:
                    fixture = dict(quote) | {'id': quote['fixture_id']}
                    confirmation = confirmation_for(observations.get(pick.fixture_id), quote, fixture, max_quote_age)
                    confirmation = confirmation or confirmation_for(quote['read_context'].get('current_confirmation'), quote, fixture, max_quote_age)
                if not quote:
                    reason = "quote_not_read_or_changed"
                elif {k: v for k, v in quote['read_context'].items() if k != 'current_confirmation'} != quote_context(quote, quote):
                    reason = 'quote_or_score_context_changed'
                elif quote["phase"] not in {"scheduled", "live"}:
                    reason = "fixture_not_active"
                elif not fresh_quote(quote, max_quote_age) and not confirmation:
                    reason = "quote_inactive_unknown_or_stale"
                if reason:
                    rejected.append({"index": index, "reason": reason})
                    continue
                baseline = {"home": quote["home_score"], "away": quote["away_score"], "half_home": quote["half_home_score"], "half_away": quote["half_away_score"]}
                new_id = uuid.uuid4()
                recorded_price = quote['price']
                result = await conn.fetchrow(
                    "INSERT INTO v2_expert_signals(id,message_id,expert_id,fixture_id,phase,market_id,market_name,period,market_kind,line,selection,recommendation_price,quote_time,confidence,evidence_text,score_baseline,score_basis) "
                    "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17) ON CONFLICT(message_id,fixture_id,market_id,period,(COALESCE(line,0)),selection) DO NOTHING RETURNING id",
                    new_id, job["message_id"], job["expert_id"], quote["fixture_id"], quote["phase"], quote["market_id"], quote["market_name"], quote["period"], quote["market_kind"], quote["line"], quote["selection"], quote["price"], quote["source_updated_at"], pick.confidence, pick.evidence_text, baseline, quote["score_basis"],
                )
                if result:
                    await conn.execute(
                        "INSERT INTO v2_quote_evidence(id,fixture_id,market_id,market_name,period,line,selection,price,evidence_kind,reference_id,source_updated_at,evidence,score_baseline) VALUES($1,$2,$3,$4,$5,$6,$7,$8,'expert_signal',$9,$10,$11,$12)",
                        uuid.uuid4(), quote["fixture_id"], quote["market_id"], quote["market_name"], quote["period"], quote["line"], quote["selection"], quote["price"], new_id, quote["source_updated_at"], {"job_id": str(body.review_id), "attempt_id": str(body.attempt_id), "quote_id": str(pick.quote_id), "score_basis": quote["score_basis"], "market_kind": quote["market_kind"], 'current_confirmation': confirmation,
                        **line_terms(quote),
                        'reviewed_price': str(quote['read_price']) if quote['read_price'] is not None else None,
                        'reviewed_source_updated_at': quote['read_source_time'].isoformat() if quote['read_source_time'] else None}, baseline,
                    )
                else:
                    existing = await conn.fetchrow("SELECT id,recommendation_price FROM v2_expert_signals WHERE message_id=$1 AND fixture_id=$2 AND market_id=$3 AND period=$4 AND selection=$6 AND market_kind=$7 AND (line IS NOT DISTINCT FROM $5::numeric OR market_kind='winner' AND COALESCE(line,0)=0 AND COALESCE($5::numeric,0)=0)", job["message_id"], quote["fixture_id"], quote["market_id"], quote["period"], quote["line"], quote["selection"], quote["market_kind"])
                    new_id, recorded_price = existing['id'], existing['recommendation_price']
                accepted.append({"index": index, "signal_id": str(new_id), "duplicate": not bool(result), "fixture_id": quote["fixture_id"], "market_name": quote["market_name"], "period": quote["period"], "line": str(quote["line"]) if quote["line"] is not None else None, "selection": quote["selection"], "price": str(recorded_price), **line_terms(quote)})
            result = {"expert": job["display_nickname"], "accepted": accepted, "rejected": rejected}
            return await tool_success(conn, body, result, result)
