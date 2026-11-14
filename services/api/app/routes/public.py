from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from .. import database
from ..pagination import decode_cursor, encode_cursor
from ..auth import get_identity
from ..membership import has_entitlement
from ..runtime_settings import runtime_quote_max_age
from ..expert_statistics import StatisticsScope, source_options, statistics_scope, summaries
from ..review_state import fresh_quote
from ..handicap_lines import line_projection
from .. import message_read

router = APIRouter(prefix="/api/v1/public")


@router.get('/client-info')
async def client_info():
    return {'product_id': 'ai-football-experts', 'api_version': 2, 'version': '2.0.0-dev',
            'min_android_version_code': 1, 'capabilities': ['fixtures', 'expert_messages', 'membership', 'private_accounts']}


def _encode_cursor(when: datetime, identifier: str, request: Request, scope) -> str:
    return encode_cursor(request.app.state.settings.session_secret, scope, when, identifier)


def _decode_cursor(value: str | None, request: Request, scope) -> tuple[datetime, str] | None:
    if not value:
        return None
    return decode_cursor(request.app.state.settings.session_secret, scope, value)


async def _has_entitlement(request: Request, key: str) -> bool:
    identity = await get_identity(request)
    if identity is None:
        return False
    if identity["role"] == "admin":
        return True
    return await has_entitlement(identity["id"], key)


async def _quote_summaries(request: Request, fixture_ids: list[str]) -> dict[str, list[dict]]:
    if not fixture_ids:
        return {}
    quote_rows = await database.current_pool().fetch(
        "SELECT q.* FROM unnest($1::text[]) AS wanted(fixture_id) CROSS JOIN LATERAL ("
        "SELECT id AS quote_id,fixture_id,market_id,market_name,period,market_kind,line,selection,price,status,is_main,source_updated_at,received_at "
        "FROM v2_current_quotes WHERE fixture_id=wanted.fixture_id "
        "ORDER BY is_main DESC,source_updated_at DESC NULLS LAST,market_id,line,selection LIMIT 24) q "
        "ORDER BY q.fixture_id,q.market_id,q.line,q.selection", fixture_ids,
    )
    by_fixture: dict[str, list[dict]] = {}
    max_quote_age = await runtime_quote_max_age(request.app.state.settings)
    for quote in quote_rows:
        by_fixture.setdefault(quote["fixture_id"], []).append(
            line_projection(quote) | {'fresh': fresh_quote(quote, max_quote_age)})
    return by_fixture


_VISIBLE_RECOMMENDATIONS_FROM = (
    "FROM v2_expert_signals s JOIN v2_expert_messages m ON m.id=s.message_id "
    "JOIN v2_experts e ON e.id=s.expert_id JOIN v2_expert_sources src ON src.id=m.source_id "
    "JOIN v2_source_experts se ON se.source_id=src.id AND se.expert_id=e.id "
)
_VISIBLE_RECOMMENDATIONS_WHERE = (
    "s.withdrawn_at IS NULL AND m.deleted_at IS NULL AND e.enabled AND src.enabled AND se.enabled "
    "AND NOT EXISTS(SELECT 1 FROM v2_expert_messages newer WHERE newer.source_id=m.source_id "
    "AND newer.platform_message_id=m.platform_message_id AND newer.revision>m.revision)"
)


async def _recommendation_summaries(fixture_ids: list[str], limit: int) -> dict[str, list[dict]]:
    if not fixture_ids:
        return {}
    rows = await database.current_pool().fetch(
        "SELECT picked.* FROM unnest($1::text[]) wanted(fixture_id) CROSS JOIN LATERAL ("
        "SELECT s.id,s.expert_id,e.display_nickname,s.fixture_id,s.phase,s.market_id,s.market_name,"
        "s.market_kind,s.period,s.line,s.selection,s.recommendation_price,s.quote_time,s.score_baseline,"
        "s.outcome,s.unit_net,s.created_at " + _VISIBLE_RECOMMENDATIONS_FROM +
        "WHERE s.fixture_id=wanted.fixture_id AND " + _VISIBLE_RECOMMENDATIONS_WHERE +
        " ORDER BY s.created_at DESC,s.id DESC LIMIT $2) picked "
        "ORDER BY picked.fixture_id,picked.created_at DESC,picked.id DESC", fixture_ids, limit+1)
    output: dict[str, list[dict]] = {}
    for row in rows:
        output.setdefault(row['fixture_id'], []).append(line_projection(row))
    return output


@router.get('/match-summaries')
async def match_summaries(request: Request, fixture_ids: list[str] = Query(min_length=1, max_length=50)):
    if any(not identifier.strip() or len(identifier) > 120 for identifier in fixture_ids):
        raise HTTPException(422, '比赛标识无效')
    identifiers = list(dict.fromkeys(fixture_ids))
    rows = await database.current_pool().fetch(
        'SELECT id,phase FROM v2_fixtures WHERE id=ANY($1::text[]) AND decision_context_only_at IS NULL', identifiers)
    phases = {row['id']: row['phase'] for row in rows}
    summaries = await _quote_summaries(request, [identifier for identifier in identifiers if identifier in phases])
    return {'items': [
        {'fixture_id': identifier, 'phase': phases[identifier],
         'markets': summaries.get(identifier, []), 'markets_status': 'loaded',
         'quote_fresh': phases[identifier] in {'scheduled', 'live'} and
                        any(quote['fresh'] for quote in summaries.get(identifier, []))}
        for identifier in identifiers if identifier in phases],
        'missing_fixture_ids': [identifier for identifier in identifiers if identifier not in phases],
        'server_time': datetime.now(timezone.utc)}


@router.get("/matches")
async def matches(
    request: Request,
    phase: str = Query(default="all", pattern="^(all|live|scheduled|finished|cancelled)$"),
    sport: str | None = Query(default=None, max_length=20),
    league: str | None = Query(default=None, max_length=100),
    league_ids: list[str] | None = Query(default=None, max_length=100),
    q: str | None = Query(default=None, max_length=120),
    match_date: date | None = Query(default=None, alias="date"),
    tz: str = Query(default='Asia/Shanghai', max_length=80),
    cursor: str | None = None,
    page_size: int = Query(default=50, ge=1, le=200),
    include_markets: bool = True,
    recommended_only: bool = False,
):
    clauses: list[str] = ['decision_context_only_at IS NULL']
    args: list[object] = []

    def bind(value: object) -> str:
        args.append(value)
        return f"${len(args)}"

    if phase != 'all':
        clauses.append(f'phase={bind(phase)}')
    else:
        clauses.append("phase IN ('live','scheduled','finished','cancelled')" if match_date else "phase IN ('live','scheduled')")
    if sport:
        clauses.append(f"lower(sport)={bind(sport.strip().lower())}")
    if league:
        clauses.append(f"competition_name ILIKE {bind('%' + league.strip() + '%')}")
    if league_ids:
        if any(not item or len(item) > 100 for item in league_ids):
            raise HTTPException(422, '联赛标识无效')
        clauses.append(f'competition_id=ANY({bind(league_ids)}::text[])')
    if q and q.strip():
        term = q.strip().lower()
        param = bind("%" + term + "%")
        alias_param = bind(term)
        clauses.append(f"(lower(home_team) LIKE {param} OR lower(away_team) LIKE {param} "
                       f"OR lower(competition_name) LIKE {param} OR EXISTS (SELECT 1 FROM "
                       f"unnest(home_aliases || away_aliases) alias WHERE lower(btrim(alias))={alias_param}))")
    if match_date:
        try:
            zone = ZoneInfo(tz)
        except ZoneInfoNotFoundError:
            raise HTTPException(422, '未知时区') from None
        start = datetime.combine(match_date, time.min, zone).astimezone(timezone.utc)
        end = datetime.combine(match_date + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
        clauses.append(f"kickoff_at >= {bind(start)} AND kickoff_at < {bind(end)}")
    if recommended_only:
        clauses.append("EXISTS(SELECT 1 " + _VISIBLE_RECOMMENDATIONS_FROM +
                       "WHERE s.fixture_id=v2_fixtures.id AND " + _VISIBLE_RECOMMENDATIONS_WHERE + ")")
    cursor_scope = {'kind': 'matches', 'phase': phase, 'sport': sport, 'league': league,
                    'league_ids': sorted(set(league_ids or [])), 'q': q, 'date': match_date, 'tz': tz}
    if recommended_only:
        cursor_scope['recommended_only'] = True
    decoded = _decode_cursor(cursor, request, cursor_scope)
    if decoded:
        clauses.append(f"(kickoff_at,id) > ({bind(decoded[0])},{bind(decoded[1])})")
    where = " AND ".join(clauses)
    args.append(page_size + 1)
    rows = await database.current_pool().fetch(
        f"SELECT id,source_id,sport,competition_id,competition_name,home_team,away_team,kickoff_at,phase,"
        f"match_state,home_score,away_score,clock_minute,half_home_score,half_away_score,source_updated_at,received_at "
        f"FROM v2_fixtures WHERE {where} ORDER BY kickoff_at,id LIMIT ${len(args)}", *args,
    )
    has_more = len(rows) > page_size
    items = rows[:page_size]
    fixture_ids = [row["id"] for row in items]
    by_fixture = await _quote_summaries(request, fixture_ids) if include_markets else {}
    full_history = await _has_entitlement(request, 'expert_full_history') if recommended_only else False
    recommendation_limit = 20 if full_history else 5
    recommendations = await _recommendation_summaries(fixture_ids, recommendation_limit) if recommended_only else {}
    output = []
    for row in items:
        item = dict(row)
        item["markets"] = by_fixture.get(row["id"], [])
        item['quote_fresh'] = any(q['fresh'] for q in item['markets']) and item['phase'] in {'scheduled','live'}
        item['markets_are_summary'] = True
        item['markets_status'] = 'loaded' if include_markets else 'not_requested'
        if recommended_only:
            item['expert_signals'] = recommendations.get(row['id'], [])[:recommendation_limit]
            item['expert_signals_has_more'] = len(recommendations.get(row['id'], [])) > recommendation_limit
            item['recommendations_limited'] = not full_history
        output.append(item)
    next_cursor = _encode_cursor(items[-1]["kickoff_at"], items[-1]["id"], request, cursor_scope) if has_more and items else None
    return {"items": output, "next_cursor": next_cursor, "page_size": page_size,
            "markets_included": include_markets, "recommended_only": recommended_only, "server_time": datetime.now(timezone.utc)}


async def _match_markets(fixture_id: str, request: Request, phase: str, *, cursor=None, page_size=500):
    scope = {'kind': 'match_markets', 'fixture_id': fixture_id}
    decoded = _decode_cursor(cursor, request, scope)
    after = None
    if decoded:
        try:
            after = uuid.UUID(decoded[1])
        except ValueError:
            raise HTTPException(400, '盘口分页游标无效') from None
    rows = await database.current_pool().fetch(
        "SELECT id AS quote_id,market_id,market_name,period,market_kind,line,selection,price,status,is_main,score_basis,source_updated_at,received_at "
        "FROM v2_current_quotes WHERE fixture_id=$1 AND EXISTS(SELECT 1 FROM v2_fixtures f WHERE f.id=$1 AND f.decision_context_only_at IS NULL) "
        "AND ($2::uuid IS NULL OR id>$2) ORDER BY id LIMIT $3",
        fixture_id, after, page_size + 1,
    )
    items = rows[:page_size]
    stale_after = await runtime_quote_max_age(request.app.state.settings)
    next_cursor = _encode_cursor(datetime(1970, 1, 1, tzinfo=timezone.utc), str(items[-1]['quote_id']),
                                 request, scope) if len(rows) > page_size else None
    return {'markets': [line_projection(row, fixture_id) | {'fresh': fresh_quote(row, stale_after) and phase in {'scheduled', 'live'}}
                        for row in items], 'next_market_cursor': next_cursor, 'market_page_size': page_size}


@router.get("/matches/{fixture_id}/markets")
async def match_markets(fixture_id: str, request: Request, cursor: str | None = None,
                        page_size: int = Query(default=500, ge=1, le=500)):
    phase = await database.current_pool().fetchval('SELECT phase FROM v2_fixtures WHERE id=$1', fixture_id)
    if phase is None:
        raise HTTPException(404, '比赛不存在')
    return await _match_markets(fixture_id, request, phase, cursor=cursor, page_size=page_size)


@router.get("/matches/{fixture_id}")
async def match_detail(fixture_id: str, request: Request):
    fixture = await database.current_pool().fetchrow(
        "SELECT id,source_id,sport,competition_id,competition_name,home_team,away_team,kickoff_at,phase,"
        "match_state,home_score,away_score,clock_minute,half_home_score,half_away_score,source_updated_at,received_at "
        "FROM v2_fixtures WHERE id=$1", fixture_id,
    )
    if not fixture:
        raise HTTPException(status_code=404, detail="比赛不存在")
    market_page = await _match_markets(fixture_id, request, fixture['phase'])
    signal_rows = await database.current_pool().fetch(
        "SELECT s.id,s.expert_id,e.display_nickname,f.sport,s.phase,f.phase AS fixture_phase,f.match_state,"
        "s.market_id,s.market_name,s.market_kind,s.period,s.score_baseline,s.line,s.selection,"
        "s.recommendation_price,s.quote_time,s.outcome,s.unit_net,s.created_at,s.withdrawn_at,s.withdrawal_reason "
        "FROM v2_expert_signals s JOIN v2_experts e ON e.id=s.expert_id "
        "JOIN v2_fixtures f ON f.id=s.fixture_id "
        "WHERE s.fixture_id=$1 ORDER BY s.created_at DESC LIMIT 100", fixture_id,
    )
    now = datetime.now(timezone.utc)
    return {"fixture": dict(fixture), **market_page, "expert_signals": [line_projection(row, fixture_id) for row in signal_rows],
            "server_time": now}


@router.get("/experts")
async def experts(scope: StatisticsScope = Depends(statistics_scope)):
    pool = database.current_pool()
    return {'items': await summaries(pool, scope.start, scope.end, scope=scope),
            'source_options': await source_options(pool, scope), **scope.response()}


@router.get("/signals")
async def signals(request: Request, cursor: str | None = None, page_size: int = Query(default=20, ge=1, le=100),
                  sport: str = Query(default='all', pattern='^(all|football|basketball)$')):
    full_history = await _has_entitlement(request, "expert_full_history")
    identity = await get_identity(request)
    if not full_history:
        page_size = min(page_size, 5)
        cursor = None
    args: list[object] = [sport]
    cursor_scope = {'kind': 'signals', **({'sport': sport} if sport != 'all' else {})}
    cursor_clause = ""
    if cursor and full_history:
        try:
            created_at, signal_id = _decode_cursor(cursor, request, cursor_scope)
        except HTTPException:
            raise
        args.extend((created_at, signal_id))
        cursor_clause = "AND (s.created_at,s.id) < ($2,$3)"
    args.append(page_size + 1)
    rows = await database.current_pool().fetch(
        "SELECT s.id,s.message_id,s.expert_id,e.display_nickname,s.fixture_id,f.sport,f.competition_name,f.home_team,f.away_team,"
        "f.kickoff_at,f.phase AS fixture_phase,f.match_state,s.phase,s.market_id,s.market_name,s.market_kind,s.period,s.line,s.selection,s.recommendation_price,s.quote_time,"
        "s.score_baseline,s.score_basis,s.withdrawn_at,s.withdrawal_reason,s.settlement_reason,s.effective_stake,"
        "(SELECT ev.evidence->>'quote_id' FROM v2_quote_evidence ev WHERE ev.reference_id=s.id AND ev.evidence_kind='expert_signal' ORDER BY ev.captured_at LIMIT 1) AS quote_id,"
        "s.confidence,s.outcome,s.unit_net,s.created_at,m.raw_text "
        "FROM v2_expert_signals s JOIN v2_experts e ON e.id=s.expert_id "
        "JOIN v2_fixtures f ON f.id=s.fixture_id JOIN v2_expert_messages m ON m.id=s.message_id "
        f"WHERE ($1::text='all' OR f.sport=$1) {cursor_clause} ORDER BY s.created_at DESC,s.id DESC LIMIT ${len(args)}", *args,
    )
    has_more = len(rows) > page_size
    items = [line_projection(row) for row in rows[:page_size]]
    if not full_history:
        for item in items:
            item.pop("raw_text", None)
            item.pop("confidence", None)
    next_cursor = _encode_cursor(items[-1]["created_at"], str(items[-1]["id"]), request, cursor_scope) if has_more and items and full_history else None
    return {"items": items, "next_cursor": next_cursor, "limited": not full_history, "signed_in": bool(identity), 'sport': sport}


@router.get("/signals/{signal_id}")
async def signal_detail(signal_id: str, request: Request):
    actor = await message_read.reader(request)
    try:
        parsed = uuid.UUID(signal_id)
    except ValueError:
        raise message_read.private_error(404, "记录不存在") from None
    row = await database.current_pool().fetchrow(
        "SELECT s.*,f.phase AS fixture_phase,f.match_state,e.display_nickname,m.raw_text,m.media_objects,m.raw_content_expired_at,m.revision AS message_revision,m.posted_at,m.source_id,j.id AS job_id,j.state AS job_state "
        "FROM v2_expert_signals s JOIN v2_experts e ON e.id=s.expert_id "
        "JOIN v2_fixtures f ON f.id=s.fixture_id "
        "JOIN v2_expert_messages m ON m.id=s.message_id LEFT JOIN LATERAL(SELECT id,state FROM v2_analysis_jobs "
        "WHERE message_id=m.id ORDER BY created_at DESC,id DESC LIMIT 1) j ON TRUE "
        "WHERE s.id=$1", parsed,
    )
    if not row:
        raise message_read.private_error(404, "记录不存在")
    item = line_projection(row)
    if item.get('raw_content_expired_at'):
        item['raw_text'] = '原文已按三天留存策略清除；解析后的专家决策继续保存。'
        item['raw_content_expired'] = True
    item['media_objects'] = message_read.message_projection({'id': item['message_id'], 'revision': item['message_revision'],
        'media_objects': item.get('media_objects')}, request=request, actor=actor)['media_objects']
    page = await message_read.trace_page(database.current_pool(), request, actor, item['message_id'])
    item.update(analysis_trace=page['items'], analysis_next_cursor=page['next_cursor'],
                analysis_has_more=page['has_more'], system_summary=await message_read.latest_summary(database.current_pool(), item['message_id']))
    return JSONResponse(jsonable_encoder(item), headers=message_read.HEADERS)


@router.get("/membership/plans")
async def membership_plans():
    rows = await database.current_pool().fetch(
        "SELECT code,name,price,currency,duration_days,entitlements FROM v2_membership_plans "
        "WHERE enabled ORDER BY price, duration_days"
    )
    return {"items": [dict(row) for row in rows]}
