"""Shared unique-team resolution over a fully read candidate snapshot."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder

from .competition_names import normalize_competition_name
from .team_matching import normalize_team_name, team_name_matches, with_verified_aliases

if TYPE_CHECKING:
    from .routes.reviews import ResolveInput

DISAMBIGUATION_LIMIT = 20
DISAMBIGUATION_FIELDS = ('id', 'sport', 'competition_name', 'home_team', 'away_team', 'kickoff_at', 'phase')


async def resolve_candidates(conn, body: ResolveInput, rows) -> dict:
    """Caller verifies the active attempt and completed, attempt-owned search."""
    matches = []
    if normalize_team_name(body.team_a) == normalize_team_name(body.team_b):
        raise HTTPException(status_code=422, detail='two distinct team identities are required')
    for row in rows:
        fixture = row['candidate_data']
        sport = fixture.get('sport')
        home_match = team_name_matches(body.team_a, fixture['home_team'], fixture.get('home_aliases', []), sport=sport)
        away_match = team_name_matches(body.team_a, fixture['away_team'], fixture.get('away_aliases', []), sport=sport)
        home_other = team_name_matches(body.team_b, fixture['home_team'], fixture.get('home_aliases', []), sport=sport)
        away_other = team_name_matches(body.team_b, fixture['away_team'], fixture.get('away_aliases', []), sport=sport)
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
    phase = await conn.fetchval(
        'WITH bound AS (INSERT INTO v2_review_fixture_bindings(job_id,attempt_id,fixture_id,search_key,evidence) '
        'VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING RETURNING fixture_id) '
        'SELECT phase FROM v2_fixtures WHERE id=$3',
        body.review_id, body.attempt_id, fixture['fixture_id'], body.search_key, evidence)
    return {'resolved': True, 'fixture': {**projected, 'phase': phase,
            'realtime_eligible': phase in {'scheduled', 'live'},
            'eligibility_reason': None if phase in {'scheduled', 'live'} else phase}}
