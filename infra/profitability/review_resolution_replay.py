"""Replay completed unbound fixture resolution against retained evidence."""

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
from inspect import signature
import json
import sys

import asyncpg
from fastapi import HTTPException
from pydantic import ValidationError

from app.config import database_url
from app.review_resolution import resolve_candidates
from app.routes.reviews import ResolveInput
from app.team_matching import team_name_matches


MATCH_USES_COMPETITION = 'competition' in signature(team_name_matches).parameters


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
    args = json.loads(row['arguments'])
    if not isinstance(args, dict) or not {'team_a', 'team_b'} <= args.keys():
        return 'invalid_resolution_parameters'
    try:
        body = ResolveInput.model_validate({**args, 'review_id': row['id'], 'attempt_id': row['attempt_id'],
                                           'search_key': row['search_key']})
    except ValidationError:
        return 'invalid_resolution_parameters'

    source = {'raw_text': row['raw_text'] or '', 'media_objects': json.loads(row['media'] or '[]')}
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


async def read(conn, begin, end):
    return await conn.fetch("""
        WITH eligible AS MATERIALIZED (
            SELECT j.id,j.attempt_id,j.message_id,r.payload->'completion_issues' ? 'text_intent_unverified' AS text_unverified
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
        SELECT e.id,e.attempt_id,e.text_unverified,m.raw_text,m.media_objects::text AS media,
               call.arguments,call.search_key,call.total_candidates,c.candidates
        FROM eligible e JOIN v2_expert_messages m ON m.id=e.message_id
        LEFT JOIN LATERAL (
            SELECT t.payload->'parameters' AS arguments,s.search_key,s.total_candidates
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
    begin, end = (datetime.fromisoformat(value) for value in sys.argv[1:])
    if not (begin.utcoffset() == end.utcoffset() == timedelta(0)
            and timedelta(0) < end-begin <= timedelta(hours=72) and end<=datetime.now(timezone.utc)):
        raise ValueError('completed UTC interval of at most 72 hours required')
    conn = await asyncpg.connect(database_url(), timeout=5, command_timeout=15)
    try:
        async with conn.transaction(readonly=True, isolation='repeatable_read'):
            rows = await read(conn, begin, end)
    finally:
        await conn.close()
    groups = Counter()
    explicit = Counter()
    for row in rows:
        reason = await classify(row)
        groups[reason] += 1
        if row['text_unverified']:
            explicit[reason] += 1
    print(json.dumps({'begin': begin.isoformat(), 'end': end.isoformat(), 'jobs': len(rows),
                      'outcomes': groups, 'explicit_intent_unverified': explicit}, sort_keys=True))


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        raise SystemExit(type(exc).__name__) from None
