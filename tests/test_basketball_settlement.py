"""Native final period checks and basketball outcomes, with no account activity."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import httpx

from app.routes import result_checks
from app.settlement import settle
from basketball_scores import SCORE_RULE_REFERENCE
from collector.leyu_protocol import LeyuError
from collector.leyu_results import normalize_result, ResultCollector
import test_result_ledger as support


def target(now=None):
    return {'fixture_id': 'leyu:5833477', 'source_match_id': '5833477', 'lease_id': str(uuid.uuid4()),
            'sport': 'basketball', 'kickoff_at': '2026-11-10T10:00:00Z',
            'home_team': '蔚山现代太阳神', 'away_team': '高阳天空枪手'}


def detail():
    # Actual authorized read on 2026-11-10, response SHA ea8782dc...4d699.
    # Identity and score fields only; unrelated statistics are deliberately absent.
    return {'mid': '5833477', 'csid': '2', 'mgt': '1794304800000',
            'mhn': '蔚山现代太阳神', 'man': '高阳天空枪手', 'mmp': '999', 'ms': 3, 'mft': 0,
            'msc': ['S1|79:86', 'S2|34:47', 'S3|45:39', 'S19|22:20',
                    'S20|12:27', 'S21|23:18', 'S22|22:21']}


def observation(raw=None, requested=None, identity=None):
    now = requested or datetime.now(timezone.utc)
    return normalize_result(raw or detail(), identity or target(), requested_at=now, observed_at=now)


def overtime_detail():
    return detail() | {'msc': ['S1|89:95', 'S2|34:47', 'S3|55:48', 'S19|22:20',
                               'S20|12:27', 'S21|23:18', 'S22|22:21', 'S7|10:9']}


def signal(market='38', **changes):
    kind, selection, line = {'37': ('winner', 'away', None), '38': ('total', 'over', '164.5'),
                             '39': ('handicap', 'away', '6.5')}[market]
    return dict(sport='basketball', market_id=market, kind=kind, selection=selection, line=line,
                period='full_time', phase='live', price='1.90', score_basis='unknown',
                score_baseline={'home': 57, 'away': 65},
                line_basis='provider_home' if market == '39' else 'selection', **changes)


class BasketballNativeResultTests(unittest.TestCase):
    def test_actual_regular_final_has_complete_periods_and_unknown_source_clock(self):
        body = observation()
        self.assertEqual((79, 86, 34, 47), tuple(body[k] for k in ('home', 'away', 'half_home', 'half_away')))
        self.assertEqual('none_verified', body['evidence']['overtime'])
        self.assertIsNone(body['source_updated_at'])
        self.assertEqual('unknown', body['evidence']['source_time_quality'])
        result_checks.ObservationInput(**body)

    def test_observed_nba_format_seven_uses_complete_quarters_not_a_guessed_format_flag(self):
        raw = detail() | {'mft': 7, 'msc': ['S1|139:132', 'S2|66:59', 'S3|73:73',
                                          'S19|28:32', 'S20|38:27', 'S21|39:35', 'S22|34:38']}
        body = observation(raw)
        self.assertEqual((139, 132), (body['home'], body['away']))
        self.assertEqual('7', body['evidence']['mft'])
        result_checks.ObservationInput(**body)

    def test_missing_duplicate_conflicting_overtime_and_nonfinal_native_scores_are_rejected(self):
        raw = detail()
        changes = [
            {'msc': raw['msc'][:-1]}, {'msc': raw['msc'] + ['S19|22:20']},
            {'msc': raw['msc'] + ['S7|?:0']},
            {'msc': raw['msc'] + ['S7|10:9', 'S7|10:9']},
            {'msc': [s.replace('S22|22:21', 'S22|?:21') for s in raw['msc']]},
            {'msc': [s.replace('S22|22:21', 'S22|22.0:21') for s in raw['msc']]},
            {'mft': None}, {'mft': 9}, {'mmp': 16}, {'ms': 1},
            {'csid': '1'}, {'mhn': 'wrong'}, {'mgt': None},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(LeyuError):
                observation(raw | change)

    def test_trusted_final_overtime_or_period_conflict_becomes_provisional(self):
        for raw in (overtime_detail(), detail() | {'msc': ['S1|89:95', *detail()['msc'][1:]]},
                    detail() | {'msc': ['S1|30:20', *detail()['msc'][1:]]}):
            with self.subTest(raw=raw):
                body = observation(raw)
                self.assertEqual('provisional', body['status'])
                self.assertEqual('full_time_unverified', body['evidence']['score_scope'])
                self.assertIn(body['evidence']['conflict_reason'],
                              {'basketball_overtime_rule_unverified', 'basketball_overtime_or_period_scores_conflict'})
                result_checks.ObservationInput(**body)
                self.assertEqual('pending', settle(signal(), body).outcome)

    def test_provisional_requires_a_complete_explicit_final_conflict(self):
        body = observation(overtime_detail())
        for proof in ({'overtime': 'unverified'}, {'conflict_reason': 'network_failure'},
                      {'quarter_4': ''}, {'state': '1'}, {'sport': '1'}, {'score_scope': 'unknown'}):
            with self.subTest(proof=proof), self.assertRaises(ValueError):
                result_checks.ObservationInput(**(body | {'evidence': body['evidence'] | proof}))
        regular = observation()
        with self.assertRaises(ValueError):
            result_checks.ObservationInput(**(regular | {'status': 'provisional'}))

    def test_persisted_observation_revalidates_quarters_scope_and_sport(self):
        body = observation()
        for proof in ({'quarter_4': '23:21'}, {'overtime': 'unknown'}, {'score_scope': 'full_time'},
                      {'mft': '9'}, {'quarter_1': ''}, {'sport': '1'}):
            with self.subTest(proof=proof), self.assertRaises(ValueError):
                result_checks.ObservationInput(**(body | {'evidence': body['evidence'] | proof}))
        for changes in ({'home': 80}, {'half_home': None}, {'away': True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                result_checks.ObservationInput(**(body | changes))


class BasketballSettlementTests(unittest.TestCase):
    def test_full_time_winner_totals_and_native_home_view_handicap(self):
        facts = observation()
        cases = [('37', {}, 'win', '.90'), ('37', {'selection': 'home'}, 'loss', '-1'),
                 ('38', {}, 'win', '.90'), ('38', {'line': '165'}, 'push', '0'),
                 ('38', {'line': '165.5'}, 'loss', '-1'),
                 ('38', {'selection': 'under'}, 'loss', '-1'),
                 ('39', {}, 'win', '.90'), ('39', {'selection': 'home'}, 'loss', '-1'),
                 ('39', {'line': '7'}, 'push', '0')]
        for phase in ('scheduled', 'live'):
            for market, changes, outcome, net in cases:
                with self.subTest(phase=phase, market=market, changes=changes):
                    selected = signal(market) | changes | {'phase': phase}
                    before = deepcopy(selected)
                    value = settle(selected, facts)
                    self.assertEqual((outcome, Decimal(net)), (value.outcome, value.unit_net))
                    self.assertTrue(value.reason.startswith(SCORE_RULE_REFERENCE + ':'))
                    self.assertEqual(before, selected)
        # The 57:65 live baseline is not subtracted as in football live handicap.
        self.assertEqual('push', settle(signal('39') | {'line': '7'}, facts).outcome)

    def test_wrong_market_period_provider_unknown_overtime_and_malformed_data_stay_pending(self):
        facts = observation()
        for changes in ({'market_id': '2'}, {'market_id': '63'}, {'period': 'quarter4'},
                        {'period': 'first_half'}, {'score_basis': 'remaining_period'},
                        {'price': 'NaN'}, {'line': '164.25'}, {'selection': 'home'}, {'phase': 'finished'}):
            self.assertEqual('pending', settle(signal() | changes, facts).outcome, changes)
        for changes in ({'evidence': {}}, {'provider_reference': 'other:5833477'}, {'status': 'provisional'},
                        {'evidence': facts['evidence'] | {'mid': '999'}},
                        {'evidence': facts['evidence'] | {'path': '/unverified'}},
                        {'evidence': facts['evidence'] | {'overtime': 'unknown'}},
                        {'evidence': facts['evidence'] | {'quarter_4': '23:21'}}, {'home': True}):
            self.assertEqual('pending', settle(signal(), facts | changes).outcome, changes)
        self.assertEqual('pending', settle(signal('37') | {'selection': 'draw'}, facts).outcome)
        self.assertEqual('void', settle(signal(), {'status': 'void'}).outcome)
        self.assertEqual('pending', settle(signal(), {'status': 'cancelled'}).outcome)


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires task-owned isolated PostgreSQL')
class BasketballResultLifecycleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ResultLedgerTests.asyncSetUp
    asyncTearDown = support.ResultLedgerTests.asyncTearDown

    async def prepare(self):
        self.app.include_router(result_checks.router)
        await self.pool.execute("UPDATE v2_fixtures SET source_id='leyu:5833477',sport='basketball',"
                                "home_team='蔚山现代太阳神',away_team='高阳天空枪手',phase='finished',kickoff_at=$1", datetime(2026, 11, 10, 10, tzinfo=timezone.utc))
        identity = await support.ResultLedgerTests.signal(self, line='165.5', phase='live')
        await self.pool.execute("UPDATE v2_expert_signals SET market_id='38' WHERE id=$1", identity)
        await self.pool.execute("UPDATE v2_quote_evidence SET market_id='38' WHERE reference_id=$1", identity)
        return identity

    async def post(self, path, body):
        return await self.client.post('/internal/collector/results/' + path, json=body,
                                      headers={'Authorization': 'Bearer isolated-collector'})

    async def claim(self):
        response = await self.post('claim', {'limit': 4})
        self.assertEqual(200, response.status_code, response.text)
        return response.json()['items'][0]

    async def test_basketball_is_claimed_settled_replayed_and_corrected_with_immutable_versions(self):
        identity = await self.prepare()
        claimed = await self.claim()
        self.assertEqual('basketball', claimed['sport'])
        first = observation(identity=claimed)
        response = await self.post('observations', first)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, response.json()['settled'])
        self.assertEqual('loss', await self.pool.fetchval('SELECT outcome FROM v2_expert_signals WHERE id=$1', identity))
        original = await self.pool.fetch('SELECT * FROM v2_signal_settlements ORDER BY result_version')
        await self.pool.execute("UPDATE v2_result_checks SET next_check_at=now()-interval '1 second'")
        replay = await self.claim()
        response = await self.post('observations', observation(identity=replay))
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(response.json()['duplicate'])
        self.assertEqual(original, await self.pool.fetch('SELECT * FROM v2_signal_settlements ORDER BY result_version'))
        await self.pool.execute("UPDATE v2_result_checks SET next_check_at=now()-interval '1 second'")
        newer = await self.claim()
        corrected = detail() | {'msc': ['S1|80:86', 'S2|34:47', 'S3|46:39', 'S19|22:20',
                                      'S20|12:27', 'S21|23:18', 'S22|23:21']}
        response = await self.post('observations', observation(corrected, identity=newer))
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(2, response.json()['version'])
        self.assertEqual(['loss', 'win'], [r['outcome'] for r in await self.pool.fetch('SELECT outcome FROM v2_signal_settlements ORDER BY result_version')])
        self.assertEqual(original[0], await self.pool.fetchrow('SELECT * FROM v2_signal_settlements WHERE result_version=1'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_cross_sport_observation_cannot_write_or_settle(self):
        await self.prepare()
        claimed = await self.claim()
        body = observation(identity=claimed)
        await self.pool.execute("UPDATE v2_fixtures SET sport='football'")
        response = await self.post('observations', body)
        self.assertEqual(422, response.status_code, response.text)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_fixture_results'))

    async def test_complete_period_conflict_is_allowed_only_on_the_native_observation_route(self):
        identity = await self.prepare()
        claimed = await self.claim()
        raw = detail() | {'msc': ['S1|30:20', *detail()['msc'][1:]]}
        body = observation(raw, identity=claimed)
        generic = body | {'source_updated_at': datetime.now(timezone.utc).isoformat()}
        response = await support.ResultLedgerTests.submit(self, generic)
        self.assertEqual(422, response.status_code, response.text)
        response = await self.post('observations', body | {'status': 'confirmed'})
        self.assertEqual(422, response.status_code, response.text)
        response = await self.post('observations', body | {'evidence': body['evidence'] | {'sport': '1'}})
        self.assertEqual(422, response.status_code, response.text)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_fixture_results'))
        response = await self.post('observations', body)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('pending', await self.pool.fetchval('SELECT outcome FROM v2_expert_signals WHERE id=$1', identity))

    async def test_football_old_provider_replay_is_rejected_and_latest_replay_is_idempotent(self):
        await support.ResultLedgerTests.signal(self)
        old = support.ResultLedgerTests.result(self, provider_reference='official-old')
        response = await support.ResultLedgerTests.submit(self, old)
        self.assertEqual(200, response.status_code, response.text)
        current = old | {'provider_reference': 'official-new', 'home': 3,
                         'source_updated_at': (self.now + timedelta(seconds=1)).isoformat()}
        response = await support.ResultLedgerTests.submit(self, current)
        self.assertEqual(200, response.status_code, response.text)
        recorded = await self.pool.fetch('SELECT * FROM v2_fixture_results ORDER BY version')
        response = await support.ResultLedgerTests.submit(self, old)
        self.assertEqual(409, response.status_code, response.text)
        response = await support.ResultLedgerTests.submit(self, current)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual((2, True), (response.json()['items'][0]['version'], response.json()['items'][0]['duplicate']))
        self.assertEqual(recorded, await self.pool.fetch('SELECT * FROM v2_fixture_results ORDER BY version'))

    async def test_native_basketball_result_enters_only_basketball_accuracy(self):
        await self.prepare()
        claimed = await self.claim()
        response = await self.post('observations', observation(identity=claimed))
        self.assertEqual(200, response.status_code, response.text)
        basketball = await self.client.get('/api/v1/public/experts', params={'sport': 'basketball'})
        football = await self.client.get('/api/v1/public/experts', params={'sport': 'football'})
        self.assertEqual((200, 200), (basketball.status_code, football.status_code))
        summary = basketball.json()['items'][0]
        self.assertEqual((1, 1, 0, -1),
                         (summary['total_signals'], summary['effective_settled_signals'],
                          summary['accuracy'], summary['unit_net']))
        self.assertEqual(0, sum(row['total_signals'] for row in football.json()['items']))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_trusted_overtime_revision_retracts_latest_statistics_replays_and_recovers(self):
        identity = await self.prepare()
        claimed = await self.claim()
        first = await self.post('observations', observation(identity=claimed))
        self.assertEqual(200, first.status_code, first.text)
        old = await self.pool.fetchrow('SELECT * FROM v2_signal_settlements WHERE result_version=1')
        await self.pool.execute("UPDATE v2_result_checks SET next_check_at=now()-interval '1 second'")
        revised = await self.claim()
        conflict = await self.post('observations', observation(overtime_detail(), identity=revised))
        self.assertEqual(200, conflict.status_code, conflict.text)
        self.assertEqual(2, conflict.json()['version'])
        state = await self.pool.fetchrow('SELECT outcome,unit_net,settled_at FROM v2_expert_signals WHERE id=$1', identity)
        self.assertEqual(('pending', None, None), tuple(state.values()))
        self.assertEqual('provisional', await self.pool.fetchval('SELECT status FROM v2_fixture_results ORDER BY version DESC LIMIT 1'))
        summary = (await self.client.get('/api/v1/public/experts', params={'sport': 'basketball'})).json()['items'][0]
        self.assertEqual((0, 1, None, 0),
                         (summary['effective_settled_signals'], summary['pending'], summary['accuracy'], summary['unit_net']))
        self.assertEqual(old, await self.pool.fetchrow('SELECT * FROM v2_signal_settlements WHERE result_version=1'))
        await self.pool.execute("UPDATE v2_result_checks SET next_check_at=now()-interval '1 second'")
        repeated = await self.claim()
        duplicate = await self.post('observations', observation(overtime_detail(), identity=repeated))
        self.assertEqual(200, duplicate.status_code, duplicate.text)
        self.assertTrue(duplicate.json()['duplicate'])
        self.assertEqual(2, await self.pool.fetchval('SELECT count(*) FROM v2_fixture_results'))
        await self.pool.execute("UPDATE v2_result_checks SET next_check_at=now()-interval '1 second'")
        recovered = await self.claim()
        confirmation = await self.post('observations', observation(identity=recovered))
        self.assertEqual(200, confirmation.status_code, confirmation.text)
        self.assertEqual(3, confirmation.json()['version'])
        self.assertEqual('loss', await self.pool.fetchval('SELECT outcome FROM v2_expert_signals WHERE id=$1', identity))
        self.assertEqual(old, await self.pool.fetchrow('SELECT * FROM v2_signal_settlements WHERE result_version=1'))
        recovered_rows = await self.pool.fetch('SELECT * FROM v2_fixture_results ORDER BY version')
        await self.pool.execute("UPDATE v2_result_checks SET next_check_at=now()-interval '1 second'")
        repeated_recovery = await self.claim()
        replay = await self.post('observations', observation(identity=repeated_recovery))
        self.assertEqual(200, replay.status_code, replay.text)
        self.assertEqual((3, True), (replay.json()['version'], replay.json()['duplicate']))
        self.assertEqual(recovered_rows, await self.pool.fetch('SELECT * FROM v2_fixture_results ORDER BY version'))
        self.assertEqual(3, len(recovered_rows))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_transport_failure_keeps_confirmed_result_and_cannot_retract_settlement(self):
        identity = await self.prepare()
        claimed = await self.claim()
        response = await self.post('observations', observation(identity=claimed))
        self.assertEqual(200, response.status_code, response.text)
        result = await self.pool.fetchrow('SELECT * FROM v2_fixture_results')
        old = await self.pool.fetchrow('SELECT * FROM v2_signal_settlements')
        await self.pool.execute("UPDATE v2_result_checks SET next_check_at=now()-interval '1 second'")
        new_claim = await self.claim()
        runtime = SimpleNamespace(version=1, request_budget=SimpleNamespace(require_available=lambda: None))
        self.client.headers['Authorization'] = 'Bearer isolated-collector'
        collector = ResultCollector(self.client, 'http://test/internal', runtime, {})
        with patch('collector.leyu_results.fetch_result', side_effect=httpx.ReadTimeout('isolated timeout')):
            value = await collector.check(new_claim, None, 1)
        self.assertEqual('result_transport_failure', value['reason'])
        self.assertEqual(result, await self.pool.fetchrow('SELECT * FROM v2_fixture_results'))
        self.assertEqual(old, await self.pool.fetchrow('SELECT * FROM v2_signal_settlements'))
        self.assertEqual('loss', await self.pool.fetchval('SELECT outcome FROM v2_expert_signals WHERE id=$1', identity))
