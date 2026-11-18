"""Replay completed unbound fixture resolution against retained evidence."""

import argparse
import asyncio
from bisect import bisect_left, bisect_right
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from inspect import signature
import json
from unittest.mock import patch

import asyncpg
from fastapi import HTTPException
from app import team_matching
from app.config import database_url
from app.review_resolution import resolve_candidates
from app.routes.reviews import ResolveInput
from app.team_matching import team_name_matches


MATCH_USES_COMPETITION = 'competition' in signature(team_name_matches).parameters
PAIR_MISSING = frozenset({'first_team_not_in_candidates', 'second_team_not_in_candidates',
                          'neither_team_in_candidates', 'teams_not_paired_in_candidates'})
PROJECTION_BEFORE = timedelta(hours=12)
PROJECTION_AFTER = timedelta(hours=24)


class ArchivedFixtureConnection:
    def __init__(self, body, source, candidates):
        self.job = body.review_id, body.attempt_id
        self.source = source
        self.phases = {row['fixture_id']: row['candidate_data']['phase'] for row in candidates}

    async def fetchrow(self, query, *args):
        if args == self.job and query.startswith('SELECT m.raw_text,m.media_objects '):
            return self.source
        if args[:2] == self.job and query.startswith('WITH current AS (SELECT phase,stale_projection_at '):
            return {'phase': self.phases[args[2]], 'stale_projection_at': None}
        raise RuntimeError('unexpected fixture resolver query')

    async def fetchval(self, query, *args):
        if args[:2] == self.job and query.startswith('WITH bound AS (INSERT INTO v2_review_fixture_bindings'):
            return self.phases[args[2]]
        raise RuntimeError('unexpected fixture resolver query')


def has_match(result):
    return result['resolved'] or result.get('candidate_count', 0) > 0


def resolve_input(row):
    args = json.loads(row['arguments'])
    if not isinstance(args, dict) or not {'team_a', 'team_b'} <= args.keys():
        raise ValueError('invalid resolution parameters')
    body = ResolveInput.model_validate({**args, 'review_id': row['id'], 'attempt_id': row['attempt_id'],
                                       'search_key': row['search_key']})
    source = {'raw_text': row['raw_text'] or '', 'media_objects': json.loads(row['media'] or '[]')}
    return body, source


def missing_pair_reason(body, candidates):
    matches = [False, False]
    for row in candidates:
        fixture = row['candidate_data']
        scope = {'sport': fixture.get('sport')}
        if MATCH_USES_COMPETITION:
            scope['competition'] = fixture.get('competition_name')
        for index, team in enumerate((body.team_a, body.team_b)):
            matches[index] |= any(team_name_matches(team, fixture[f'{side}_team'],
                fixture.get(f'{side}_aliases', []), **scope) for side in ('home', 'away'))
    return {(False, False): 'neither_team_in_candidates', (True, False): 'second_team_not_in_candidates',
            (False, True): 'first_team_not_in_candidates', (True, True): 'teams_not_paired_in_candidates'}[tuple(matches)]


async def classify(row):
    if row['arguments'] is None:
        return 'no_completed_resolution_call'
    candidates = json.loads(row['candidates'] or '[]')
    if len(candidates) != row['total_candidates']:
        return 'candidate_snapshot_incomplete'
    try:
        body, source = resolve_input(row)
    except ValueError:
        return 'invalid_resolution_parameters'

    if not source['raw_text'] and not source['media_objects']:
        return 'source_evidence_unavailable'
    conn = ArchivedFixtureConnection(body, source, candidates)
    try:
        result = await resolve_candidates(conn, body, candidates)
    except HTTPException as exc:
        if exc.status_code == 422:
            return 'invalid_team_pair'
        raise
    if result['resolved']:
        return 'unique_in_archived_snapshot'
    if result['reason'] != 'no_matching_fixture':
        return result['reason']

    refinements = ({'source_phase': None} if 'source_phase' in type(body).model_fields else {})
    refinements.update(competition=None, from_utc=None, to_utc=None)
    params = body.model_copy(update=refinements)
    if not has_match(await resolve_candidates(conn, params, candidates)):
        return missing_pair_reason(body, candidates)
    for stage, fields in (('phase', ('source_phase',)), ('competition', ('competition',)),
                          ('kickoff', ('from_utc', 'to_utc'))):
        params = params.model_copy(update={field: getattr(body, field, None) for field in fields if field in refinements})
        if not has_match(await resolve_candidates(conn, params, candidates)):
            return stage + '_filter_removed_all'
    return 'replay_inconsistent'


async def classify_alternates(row, searches):
    body, source = resolve_input(row)
    available = False
    incomplete = False
    ambiguous = False
    resolved = set()
    for search in searches:
        if search['search_key'] == row['search_key']:
            continue
        candidates = json.loads(search['candidates'] or '[]')
        if len(candidates) != search['total_candidates']:
            incomplete = True
            continue
        available = True
        alternate = body.model_copy(update={'search_key': search['search_key']})
        conn = ArchivedFixtureConnection(alternate, source, candidates)
        result = await resolve_candidates(conn, alternate, candidates)
        if result['resolved']:
            resolved.add(result['fixture']['id'])
        elif result['reason'] == 'ambiguous_fixture':
            ambiguous = True
    if ambiguous or len(resolved) > 1:
        return 'alternate_search_ambiguity'
    if resolved:
        return 'unique_in_alternate_search'
    if available:
        return 'no_match_in_alternate_search'
    return 'alternate_snapshot_incomplete' if incomplete else 'no_other_complete_nonempty_search'


async def read_alternates(conn, ids):
    records = await conn.fetch("""
        SELECT s.job_id,s.search_key,s.total_candidates,c.candidates
        FROM v2_analysis_jobs j JOIN v2_match_searches s ON s.job_id=j.id AND s.attempt_id=j.attempt_id
        CROSS JOIN LATERAL (
            SELECT jsonb_agg(jsonb_build_object('fixture_id',c.fixture_id,'candidate_data',c.candidate_data)
               ORDER BY c.position) AS candidates
            FROM v2_match_search_candidates c WHERE c.job_id=s.job_id AND c.search_key=s.search_key
        ) c WHERE j.id=ANY($1::uuid[]) AND s.complete AND s.total_candidates>0
        ORDER BY s.job_id,s.search_key
    """, ids)
    groups = {}
    for row in records:
        groups.setdefault(row['job_id'], []).append(row)
    return groups


@lru_cache(maxsize=100_000)
def catalog_team_matches(team, name, aliases, sport, competition):
    scope = {'sport': sport}
    if MATCH_USES_COMPETITION:
        scope['competition'] = competition
    return team_name_matches(team, name, aliases, **scope)


async def classify_projection(row, projection, kickoff_times):
    body, source = resolve_input(row)
    window_begin = row['created_at'] - PROJECTION_BEFORE
    window_end = row['created_at'] + PROJECTION_AFTER
    begin = max(body.from_utc or window_begin, window_begin)
    end = min(body.to_utc or window_end, window_end)
    if begin > end:
        return 'outside_projection_review_window'

    matches = []
    for provider_row in projection[bisect_left(kickoff_times, begin):bisect_right(kickoff_times, end)]:
        scope = provider_row['sport'], provider_row['competition_name']
        home = provider_row['home_team'], tuple(provider_row['home_aliases'])
        away = provider_row['away_team'], tuple(provider_row['away_aliases'])
        if ((catalog_team_matches(body.team_a, *home, *scope) and catalog_team_matches(body.team_b, *away, *scope))
                or (catalog_team_matches(body.team_a, *away, *scope) and catalog_team_matches(body.team_b, *home, *scope))):
            matches.append(provider_row)
    if not matches:
        return 'no_pair_in_current_projection'
    candidates = [{
        'fixture_id': item['id'],
        'candidate_data': {**item, 'kickoff_at': item['kickoff_at'].isoformat()},
    } for item in matches]
    conn = ArchivedFixtureConnection(body, source, candidates)
    result = await resolve_candidates(conn, body, candidates)
    if not result['resolved']:
        return 'current_projection_' + result['reason']
    match = next(item for item in matches if item['id'] == result['fixture']['id'])
    if match['received_at'] <= row['call_at']:
        return 'current_unique_projection_received_before_call'
    return 'current_unique_projection_latest_receipt_after_call'


async def read_projection(conn, begin, end):
    rows = await conn.fetch("""
        SELECT id,sport,competition_name,home_team,away_team,home_aliases,away_aliases,kickoff_at,phase,received_at
        FROM v2_fixtures WHERE kickoff_at BETWEEN $1 AND $2 AND decision_context_only_at IS NULL
        ORDER BY kickoff_at,id
    """, begin - PROJECTION_BEFORE, end + PROJECTION_AFTER)
    return [dict(row) for row in rows]


async def read(conn, begin, end):
    return await conn.fetch("""
        WITH eligible AS MATERIALIZED (
            SELECT j.id,j.attempt_id,j.message_id,j.created_at,
                   r.payload->'completion_issues' ? 'text_intent_unverified' AS text_unverified
            FROM v2_analysis_jobs j JOIN LATERAL (
                SELECT payload FROM v2_analysis_traces t WHERE t.job_id=j.id AND t.trace_kind='system_review_result'
                  AND t.payload->>'attempt_id'=j.attempt_id::text ORDER BY t.sequence_no DESC LIMIT 1
            ) r ON true
            WHERE j.created_at>=$1 AND j.created_at<$2 AND j.state='unresolved' AND j.review_mode='real_time'
              AND NOT EXISTS (SELECT 1 FROM v2_review_fixture_bindings b WHERE b.job_id=j.id AND b.attempt_id=j.attempt_id)
              AND EXISTS (SELECT 1 FROM v2_match_searches s WHERE s.job_id=j.id AND s.attempt_id=j.attempt_id
                          AND s.complete AND s.total_candidates>0)
              AND NOT EXISTS (SELECT 1 FROM v2_analysis_traces t WHERE t.job_id=j.id
                  AND t.payload->>'attempt_id'=j.attempt_id::text AND t.trace_kind IN (
                    'mcp_list_match_markets','mcp_record_expert_signals'))
        )
        SELECT e.id,e.attempt_id,e.created_at,e.text_unverified,m.raw_text,m.media_objects::text AS media,
               call.arguments,call.search_key,call.total_candidates,call.call_at,c.candidates
        FROM eligible e JOIN v2_expert_messages m ON m.id=e.message_id
        LEFT JOIN LATERAL (
            SELECT t.payload->'parameters' AS arguments,s.search_key,s.total_candidates,t.created_at AS call_at
            FROM v2_analysis_traces t JOIN v2_match_searches s
              ON s.job_id=e.id AND s.attempt_id=e.attempt_id AND s.search_key=CASE
                    WHEN t.trace_kind='mcp_search_matches' THEN t.payload->>'search_key'
                    ELSE t.payload->'parameters'->>'search_key' END
            WHERE t.job_id=e.id AND t.payload->>'attempt_id'=e.attempt_id::text AND t.payload->>'status'='succeeded'
              AND t.payload->'parameters'->>'team_a' IS NOT NULL AND t.payload->'parameters'->>'team_b' IS NOT NULL
              AND (t.trace_kind='mcp_resolve_match' OR t.trace_kind='mcp_search_matches' AND t.payload->>'complete'='true')
              AND s.complete AND s.total_candidates>0
            ORDER BY t.sequence_no DESC LIMIT 1
        ) call ON true
        LEFT JOIN LATERAL (
            SELECT jsonb_agg(jsonb_build_object('fixture_id',c.fixture_id,'candidate_data',c.candidate_data)
                ORDER BY c.position) AS candidates
            FROM v2_match_search_candidates c WHERE c.job_id=e.id AND c.search_key=call.search_key
        ) c ON call.search_key IS NOT NULL
        ORDER BY e.id
    """, begin, end)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('begin', type=datetime.fromisoformat)
    parser.add_argument('end', type=datetime.fromisoformat)
    parser.add_argument('--projection', action='store_true')
    args = parser.parse_args()
    begin, end = args.begin, args.end
    if not (begin.utcoffset() == end.utcoffset() == timedelta(0)
            and timedelta(0) < end-begin <= timedelta(hours=72) and end<=datetime.now(timezone.utc)):
        raise ValueError('completed UTC interval of at most 72 hours required')
    conn = await asyncpg.connect(database_url(), timeout=5, command_timeout=15)
    try:
        async with conn.transaction(readonly=True, isolation='repeatable_read'):
            rows = await read(conn, begin, end)
            searches = await read_alternates(conn, [row['id'] for row in rows if row['arguments'] is not None])
            projection = await read_projection(conn, begin, end) if args.projection else []
    finally:
        await conn.close()
    groups = Counter()
    explicit = Counter()
    alternate_groups = Counter()
    explicit_alternates = Counter()
    projection_groups = Counter()
    kickoff_times = [fixture['kickoff_at'] for fixture in projection]
    for row in rows:
        reason = await classify(row)
        groups[reason] += 1
        if row['text_unverified']:
            explicit[reason] += 1
        if reason in PAIR_MISSING:
            alternate = await classify_alternates(row, searches.get(row['id'], ()))
            alternate_groups[alternate] += 1
            if row['text_unverified']:
                explicit_alternates[alternate] += 1
            if args.projection:
                projection_groups[await classify_projection(row, projection, kickoff_times)] += 1
    print(json.dumps({'begin': begin.isoformat(), 'end': end.isoformat(), 'jobs': len(rows),
                      'outcomes': groups, 'explicit_intent_unverified': explicit,
                      'alternate_searches': alternate_groups,
                      'explicit_intent_alternate_searches': explicit_alternates,
                      **({'current_projection': projection_groups} if args.projection else {})}, sort_keys=True))


if __name__ == '__main__':
    try:
        # This executable runs in its own process. Preserve the deployed
        # comparison while avoiding repeated registry lookups for each pair.
        with patch.object(team_matching, '_name_forms', lru_cache(maxsize=32_768)(team_matching._name_forms)):
            asyncio.run(main())
    except Exception as exc:
        raise SystemExit(type(exc).__name__) from None
