"""Shared unique-team resolution over a fully read candidate snapshot."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder

from .team_matching import normalize_team_name, team_name_matches

if TYPE_CHECKING:
    from .routes.reviews import ResolveInput


async def resolve_candidates(conn, body: ResolveInput, rows) -> dict:
    """Caller verifies the active attempt and completed, attempt-owned search."""
    matches = []
    if normalize_team_name(body.team_a) == normalize_team_name(body.team_b):
        raise HTTPException(status_code=422, detail='two distinct team identities are required')
    for row in rows:
        fixture = row['candidate_data']
        home_match = team_name_matches(body.team_a, fixture['home_team'], fixture.get('home_aliases', []))
        away_match = team_name_matches(body.team_a, fixture['away_team'], fixture.get('away_aliases', []))
        home_other = team_name_matches(body.team_b, fixture['home_team'], fixture.get('home_aliases', []))
        away_other = team_name_matches(body.team_b, fixture['away_team'], fixture.get('away_aliases', []))
        if not ((home_match and away_other) or (away_match and home_other)):
            continue
        if body.competition and normalize_team_name(body.competition) != normalize_team_name(fixture['competition_name']):
            continue
        kickoff = datetime.fromisoformat(fixture['kickoff_at'])
        if body.from_utc and kickoff < body.from_utc or body.to_utc and kickoff > body.to_utc:
            continue
        matches.append(row)
    if len(matches) != 1:
        return {'resolved': False, 'reason': 'no_matching_fixture' if not matches else 'ambiguous_fixture',
                'candidate_count': len(matches)}
    fixture = matches[0]
    evidence = jsonable_encoder(body.model_dump(exclude={'review_id', 'attempt_id'}))
    phase = await conn.fetchval(
        'WITH bound AS (INSERT INTO v2_review_fixture_bindings(job_id,attempt_id,fixture_id,search_key,evidence) '
        'VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING RETURNING fixture_id) '
        'SELECT phase FROM v2_fixtures WHERE id=$3',
        body.review_id, body.attempt_id, fixture['fixture_id'], body.search_key, evidence)
    return {'resolved': True, 'fixture': {**fixture['candidate_data'], 'phase': phase,
            'realtime_eligible': phase in {'scheduled', 'live'},
            'eligibility_reason': None if phase in {'scheduled', 'live'} else phase}}
