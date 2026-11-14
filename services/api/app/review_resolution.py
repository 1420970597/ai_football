"""Shared unique-team resolution over a fully read candidate snapshot."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder

from .competition_names import normalize_competition_name
from .team_matching import normalize_team_name, team_name_matches, with_verified_aliases
from .review_team_identity import added_squad_markers

if TYPE_CHECKING:
    from .routes.reviews import ResolveInput

DISAMBIGUATION_LIMIT = 20
DISAMBIGUATION_FIELDS = ('id', 'sport', 'competition_name', 'home_team', 'away_team', 'kickoff_at', 'phase')


async def resolve_candidates(conn, body: ResolveInput, rows) -> dict:
    """Caller verifies the active attempt and completed, attempt-owned search."""
    # The source text is authoritative negative evidence. A later model turn
    # must not adopt a candidate's reserve/women/youth qualifier absent from
    # a text-only source. Images need their own visual evidence and are not
    # interpreted by this guard.
    source=await conn.fetchrow(
        'SELECT m.raw_text,m.media_objects FROM v2_analysis_jobs j '
        'JOIN v2_expert_messages m ON m.id=j.message_id WHERE j.id=$1 AND j.attempt_id=$2',
        body.review_id,body.attempt_id)
    if source and not source['media_objects'] and added_squad_markers(body.team_a,body.team_b,source['raw_text']):
        return {'resolved':False,'reason':'source_squad_identity_unverified','candidate_count':0}
    matches = []
    if normalize_team_name(body.team_a) == normalize_team_name(body.team_b):
        raise HTTPException(status_code=422, detail='two distinct team identities are required')
    for row in rows:
        fixture = row['candidate_data']
        # Only an explicit source claim can refine a completed all-phase
        # search. Eligibility alone never proves which fixture was intended.
        if body.source_phase is not None and fixture.get('phase') != body.source_phase:
            continue
        sport = fixture.get('sport')
        identity_scope = {'sport': sport, 'competition': fixture.get('competition_name')}
        home_match = team_name_matches(body.team_a, fixture['home_team'], fixture.get('home_aliases', []), **identity_scope)
        away_match = team_name_matches(body.team_a, fixture['away_team'], fixture.get('away_aliases', []), **identity_scope)
        home_other = team_name_matches(body.team_b, fixture['home_team'], fixture.get('home_aliases', []), **identity_scope)
        away_other = team_name_matches(body.team_b, fixture['away_team'], fixture.get('away_aliases', []), **identity_scope)
        if not ((home_match and away_other) or (away_match and home_other)):
            continue
        if body.competition:
            sport = fixture.get('sport')
            if normalize_competition_name(body.competition, sport) != normalize_competition_name(fixture['competition_name'], sport):
                continue
        kickoff = datetime.fromisoformat(fixture['kickoff_at'])
        if body.from_utc and kickoff < body.from_utc or body.to_utc and kickoff > body.to_utc:
            continue
        matches.append(row)
    if len(matches) != 1:
        result = {'resolved': False, 'reason': 'no_matching_fixture' if not matches else 'ambiguous_fixture',
                  'candidate_count': len(matches)}
        if matches:
            ordered = sorted(matches, key=lambda row: (row['candidate_data']['kickoff_at'], row['fixture_id']))
            result['matching_candidates'] = [
                {key: row['candidate_data'].get(key) for key in DISAMBIGUATION_FIELDS}
                for row in ordered[:DISAMBIGUATION_LIMIT]]
            result['matching_candidates_complete'] = len(matches) <= DISAMBIGUATION_LIMIT
        return result
    fixture = matches[0]
    evidence = jsonable_encoder(body.model_dump(exclude={'review_id', 'attempt_id'}))
    projected = with_verified_aliases(fixture['candidate_data'])
    if 'verified_alias_sources' in projected:
        evidence['verified_alias_sources'] = projected['verified_alias_sources']
    bound = await conn.fetchrow(
        'WITH current AS (SELECT phase,stale_projection_at FROM v2_fixtures WHERE id=$3), '
        'bound AS (INSERT INTO v2_review_fixture_bindings(job_id,attempt_id,fixture_id,search_key,evidence) '
        'SELECT $1,$2,$3,$4,$5 FROM current WHERE $6::text IS NULL OR phase=$6 '
        'ON CONFLICT DO NOTHING RETURNING fixture_id) '
        'SELECT phase,stale_projection_at FROM current',
        body.review_id, body.attempt_id, fixture['fixture_id'], body.search_key, evidence, body.source_phase)
    phase = bound['phase'] if bound is not None else None
    if body.source_phase is not None and phase != body.source_phase:
        return {'resolved': False, 'reason': 'source_phase_changed', 'candidate_count': 0}
    # A binding may still be created for a row the source stopped reporting, so
    # the recommendation can be explained, but such a row is never eligible for
    # a new live signal.
    eligible = phase in {'scheduled', 'live'} and (bound is None or bound['stale_projection_at'] is None)
    # Preserve which source name proved each side; a composition must never
    # assume that source order equals provider home/away order. Overlapping
    # names can identify one fixture without proving a unique orientation.
    source_sides = None
    identity_scope = {'sport': projected.get('sport'), 'competition': projected.get('competition_name')}
    home_a = team_name_matches(body.team_a, projected['home_team'], projected.get('home_aliases', []), **identity_scope)
    away_a = team_name_matches(body.team_a, projected['away_team'], projected.get('away_aliases', []), **identity_scope)
    home_b = team_name_matches(body.team_b, projected['home_team'], projected.get('home_aliases', []), **identity_scope)
    away_b = team_name_matches(body.team_b, projected['away_team'], projected.get('away_aliases', []), **identity_scope)
    if (home_a and away_b) != (away_a and home_b):
        source_sides = ({'team_a': 'home', 'team_b': 'away'} if home_a and away_b
                        else {'team_a': 'away', 'team_b': 'home'})
    return {'resolved': True, 'source_team_sides': source_sides, 'fixture': {**projected, 'phase': phase,
            'realtime_eligible': eligible,
            'eligibility_reason': None if eligible else
            ('source_stopped_reporting' if phase in {'scheduled', 'live'} else phase)}}
