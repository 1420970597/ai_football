"""Independent decisions, immutable timestamps and safe historical repairs."""
import tempfile
import json
import threading
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

from core.settlement import settle_pick, split_line, summarise
from service.ledger import DecisionLedger, LedgerEntry


class LedgerRegressionTests(unittest.TestCase):
    def test_hot_evidence_imports_active_revisions_without_scanning_archive(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = DecisionLedger(root)
            entry = self.entry(algorithm='economic_ensemble', trigger='live_recommendation')
            ledger._append([entry])
            self.assertEqual(ledger.recommendation_performance('OU','2.25','over')['pending_count'], 1)
            revised = replace(entry, status='won', pnl=1, updated_at='2026-01-02T00:00:00Z')
            with ledger.path.open('a') as stream:
                stream.write(json.dumps(revised.as_dict())+'\n')
            with patch.object(ledger, '_files', side_effect=AssertionError('archive scan in execution')):
                result = ledger.recommendation_performance('OU','2.25','over')
            self.assertEqual(result['settled_samples'], 1)
            self.assertEqual(result['hit_count'], 1)
            self.assertEqual(result['pending_count'], 0)

    def test_live_batch_retains_each_market_once_and_retries_failed_write(self):
        from tests.test_live_expert import LiveExpertTests
        from service.live_expert import LiveExpertService
        hub = LiveExpertTests().hub()
        row = LiveExpertService().compute(hub.decision_snapshot('m'), hub)
        forecasts = row['evaluations'][0]['forecasts']
        inputs = [{**row, 'algorithm': row['evaluations'][0]['algorithm'], 'forecast': f} for f in forecasts]
        self.assertGreater(len(inputs), 1)
        with tempfile.TemporaryDirectory() as root:
            ledger = DecisionLedger(root)
            with patch.object(ledger, '_append', return_value=0):
                with self.assertRaises(OSError):
                    ledger.record_live_many(inputs)
            self.assertEqual(ledger.record_live_many(inputs), len(inputs))
            self.assertEqual(ledger.record_live_many(inputs), 0)
            self.assertEqual(len(ledger.load()), len(inputs))

    def test_numeric_quarters_equal_split_notation(self):
        for n in range(-16, 17):
            line = n / 4
            parts = split_line(line)
            self.assertTrue(parts)
            for home in range(4):
                for away in range(4):
                    for outcome in ('home', 'away'):
                        expected = settle_pick('AH', outcome, '/'.join(map(str, parts)), (home, away))
                        self.assertEqual(settle_pick('AH', outcome, line, (home, away)), expected)
        self.assertEqual(settle_pick('OU', 'over', '2.25', (1, 1))[0], 'half_lost')
        self.assertEqual(settle_pick('AH', 'home', '-.25', (0, 0))[0], 'half_lost')

    def test_invalid_data_is_not_zero_score(self):
        for score in ({}, {'home': 1}, (-1, 2), (True, 0), (1.1, 2), (float('inf'), 2)):
            self.assertEqual(settle_pick('HAD', 'draw', '', score)[0], 'void')
        for line in ('nan', 'inf', '2.1', '1/3', '1/2/3', True):
            self.assertEqual(split_line(line), [])

    def entry(self, identity='a', **kwargs):
        values = dict(at='2026-01-01T12:00:00+00:00', match_id='m', market='OU',
                      line='2.25', outcome='over', odds=2, is_pick=True, decision_id=identity)
        values.update(kwargs)
        return LedgerEntry(**values)

    def test_revisions_keep_all_decisions_and_original_dates(self):
        with tempfile.TemporaryDirectory() as root:
            led = DecisionLedger(root)
            led._append([self.entry('a'), self.entry('b', at='2026-01-02T12:00:00+00:00')])
            led.capture_closing({('m', 'OU', '2.25', 'over'): 1.9})
            rows = led.load()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0].date_key, '2026-01-01')
            self.assertEqual(rows[1].date_key, '2026-01-02')
            self.assertTrue(rows[0].updated_at)
            led.settle({'m': {'ft': [1, 1], 'done': True}})
            self.assertEqual([r.status for r in led.load()], ['half_lost', 'half_lost'])
            self.assertEqual(led.stats()['profit_units'], -1)

    def test_live_result_and_invalid_score_remain_pending(self):
        with tempfile.TemporaryDirectory() as root:
            led = DecisionLedger(root)
            led._append([self.entry()])
            for score in ({'ft': [1, 1], 'done': False}, {'ft': [-1, 1]}, {'ft': []}):
                self.assertEqual(led.settle({'m': score})['settled'], 0)
                self.assertEqual(led.load()[0].status, 'pending')

    def test_archive_and_regrade_idempotence(self):
        with tempfile.TemporaryDirectory() as root:
            led = DecisionLedger(root)
            led._append([self.entry(status='lost', pnl=-1)])
            led.path.rename(Path(root) / 'ledger.20260101.jsonl')
            led.settle({'m': {'ft': [1, 1]}}, regrade=True)
            for _ in range(2):
                led.settle({'m': {'ft': [1, 1]}}, regrade=True)
                rows = led.load()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].pnl, -.5)

    def test_new_append_waits_for_settlement_without_being_lost(self):
        with tempfile.TemporaryDirectory() as root:
            led = DecisionLedger(root)
            led._append([self.entry()])
            original = led._split_score
            started = threading.Event()
            release = threading.Event()

            def gated(score):
                started.set()
                self.assertTrue(release.wait(3))
                return original(score)

            with patch.object(led, '_split_score', side_effect=gated):
                worker = threading.Thread(target=led.settle, args=({'m': {'ft': [1, 1]}},))
                worker.start()
                self.assertTrue(started.wait(3))
                writer = threading.Thread(target=led._append, args=([self.entry('new')],))
                writer.start()
                release.set()
                worker.join(3)
                writer.join(3)
            self.assertEqual(len(led.load()), 2)

    def test_history_actual_time_order(self):
        with tempfile.TemporaryDirectory() as root:
            led = DecisionLedger(root)
            led._append([self.entry('a', at='2026-01-01T20:59:00+00:00'),
                         self.entry('b', at='2026-01-02T01:00:00+00:00')])
            self.assertEqual(led.history()['entries'][0]['decision_id'], 'b')

    def test_half_stake_rate_and_inplay_clv(self):
        rows = [dict(status='won', is_pick=True, pnl=1, odds=2),
                dict(status='half_lost', is_pick=True, pnl=-.5, odds=2,
                     closing_odds=1.8, is_live=True)]
        st = summarise(rows)
        self.assertAlmostEqual(st['hit_rate'], .6667)
        self.assertEqual(st['profitable_pick_rate'], .5)
        self.assertEqual(st['roi'], .25)
        self.assertEqual(st['clv_n'], 0)

    def test_remaining_score_handicap_basis(self):
        with tempfile.TemporaryDirectory() as root:
            led = DecisionLedger(root)
            led._append([self.entry(market='AH', line='-.25', outcome='home',
                                   settlement_basis='remaining_score', entry_score=[2, 0])])
            led.settle({'m': {'ft': [2, 0]}})
            self.assertEqual(led.load()[0].status, 'half_lost')
