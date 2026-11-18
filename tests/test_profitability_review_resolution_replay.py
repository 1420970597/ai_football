import json
import unittest
from uuid import UUID
from datetime import datetime, timedelta

from infra.profitability.review_resolution_replay import classify, classify_alternates, classify_projection


def candidate(fixture_id, home, away, **changes):
    return {'fixture_id': fixture_id, 'candidate_data': {
        'id': fixture_id, 'sport': 'football', 'home_team': home, 'away_team': away,
        'competition_name': 'Premier Division', 'kickoff_at': '2026-11-17T02:00:00Z',
        'phase': 'scheduled', **changes,
    }}


class ReviewResolutionReplayTests(unittest.IsolatedAsyncioTestCase):
    def row(self, candidates, **params):
        return {'id': UUID(int=1), 'attempt_id': UUID(int=2), 'search_key': 'a' * 64,
                'total_candidates': len(candidates), 'candidates': json.dumps(candidates),
                'arguments': json.dumps({'team_a': 'Alpha FC', 'team_b': 'Beta FC', **params}),
                'raw_text': 'Match preview', 'media': '[]'}

    async def test_missing_sides_are_distinct(self):
        first = candidate('1', 'Alpha FC', 'Gamma FC')
        second = candidate('2', 'Delta FC', 'Beta FC')
        neither = candidate('3', 'Gamma FC', 'Delta FC')
        for candidates, expected in (
            ([first], 'second_team_not_in_candidates'),
            ([second], 'first_team_not_in_candidates'),
            ([neither], 'neither_team_in_candidates'),
            ([first, second], 'teams_not_paired_in_candidates'),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(await classify(self.row(candidates)), expected)

    async def test_resolver_applies_archived_competition_and_time_filters(self):
        candidates = [candidate('1', 'Alpha FC', 'Beta FC')]
        self.assertEqual(await classify(self.row(candidates)), 'unique_in_archived_snapshot')
        self.assertEqual(await classify(self.row(candidates, competition='Second Division')),
                         'competition_filter_removed_all')
        self.assertEqual(await classify(self.row(candidates, from_utc='2026-11-17T03:00:00Z')),
                         'kickoff_filter_removed_all')
        candidates.append(candidate('2', 'Alpha FC', 'Beta FC', kickoff_at='2026-11-18T02:00:00Z'))
        self.assertEqual(await classify(self.row(candidates)), 'ambiguous_fixture')

    async def test_missing_archive_and_invalid_parameters_do_not_replay(self):
        row = self.row([candidate('1', 'Alpha FC', 'Beta FC')])
        self.assertEqual(await classify(dict(row, total_candidates=2)), 'candidate_snapshot_incomplete')
        self.assertEqual(await classify(dict(row, arguments=None)), 'no_completed_resolution_call')
        self.assertEqual(await classify(dict(row, arguments='{}')), 'invalid_resolution_parameters')
        self.assertEqual(await classify(dict(row, arguments='[]')), 'invalid_resolution_parameters')
        self.assertEqual(await classify(dict(row, raw_text='')), 'source_evidence_unavailable')

    async def test_alternate_searches_require_complete_evidence_and_consistency(self):
        initial = self.row([candidate('1', 'Alpha FC', 'Gamma FC')])
        other = dict(self.row([candidate('2', 'Alpha FC', 'Beta FC')]), search_key='b' * 64)
        self.assertEqual(await classify_alternates(initial, [initial]), 'no_other_complete_nonempty_search')
        self.assertEqual(await classify_alternates(initial, [dict(other, total_candidates=2)]),
                         'alternate_snapshot_incomplete')
        self.assertEqual(await classify_alternates(initial, [initial, other]), 'unique_in_alternate_search')
        conflict = dict(self.row([candidate('3', 'Alpha FC', 'Beta FC')]), search_key='c' * 64)
        self.assertEqual(await classify_alternates(initial, [other, conflict]), 'alternate_search_ambiguity')

    async def test_alternate_search_retains_original_competition_and_time_filters(self):
        initial = self.row([candidate('1', 'Alpha FC', 'Gamma FC')], competition='Premier Division',
                           from_utc='2026-11-17T03:00:00Z')
        other = dict(self.row([candidate('2', 'Alpha FC', 'Beta FC')]), search_key='b' * 64)
        self.assertEqual(await classify_alternates(initial, [other]), 'no_match_in_alternate_search')

    async def test_projection_distinguishes_before_call_and_later_observations(self):
        review_at = datetime.fromisoformat('2026-11-17T01:00:00Z')
        initial = dict(self.row([]), created_at=review_at, call_at=review_at)
        fixture = candidate('1', 'Alpha FC', 'Beta FC')['candidate_data']
        fixture.update(kickoff_at=datetime.fromisoformat(fixture['kickoff_at']),
                       home_aliases=[], away_aliases=[], received_at=review_at - timedelta(minutes=2))
        kickoff = [fixture['kickoff_at']]
        self.assertEqual(await classify_projection(initial, [fixture], kickoff),
                         'current_unique_projection_received_before_call')
        fixture['received_at'] = review_at + timedelta(minutes=2)
        self.assertEqual(await classify_projection(initial, [fixture], kickoff),
                         'current_unique_projection_latest_receipt_after_call')
        fixture['away_team'] = 'Gamma FC'
        self.assertEqual(await classify_projection(initial, [fixture], kickoff), 'no_pair_in_current_projection')
