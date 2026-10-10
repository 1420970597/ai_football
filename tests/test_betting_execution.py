import json
import sqlite3
import tempfile
import time
import unittest
from dataclasses import replace
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

from collector.leyu_account import BetSubmissionRejected, BetSubmissionUnknown
from collector.leyu_realtime import LiveQuote, parse_c105
from service.analysis import AnalysisConfig, AnalysisService
from service.betting import BettingExecutor
from service.runtime_settings import RuntimeConfig, RuntimeSettings
from tests import test_live_expert


class BettingExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = RuntimeSettings(replace(RuntimeConfig(), betting_enabled=True, betting_fixed_stake=2))
        detail = {'matchId': 'm', 'marketId': 'market1', 'playId': '2',
                  'playOptions': 'Over', 'playOptionsId': 'option1',
                  'marketValue': '2.5', 'oddFinally': '1.95', 'sportId': '1', 'matchType': 2}
        self.pick = {'market': 'OU', 'line': '2.5', 'outcome': 'over', 'confidence': .8,
                     'odds': 1.95, 'algorithm': 'economic_ensemble', 'order_detail': detail}
        self.row = {'match_id': 'm', 'algorithm': 'economic_ensemble', 'competition_type': 'real',
                    'config_version': 1, 'version': 10, 'published_at_ms': time.time() * 1000,
                    'picks': [self.pick]}
        self.snapshot = {'version': 10, 'score': (1, 0), 'score_age_s': 0, 'status_age_s': 0,
                         'status': {'mmp': '7'}, 'finished': False, 'suspended': False,
                         'quotes': [LiveQuote('m', '2', '2.5', 'option1', 'Over', 1.95,
                                              int(time.time() * 1000))]}
        self.hub = MagicMock()
        self.hub.decision_snapshot.side_effect = lambda mid: dict(self.snapshot)
        self.ledger = MagicMock()
        self.ledger.recommendation_performance.return_value = {'hit_count': 4, 'settled_samples': 6}
        self.client = MagicMock()
        self.client.prepare_bet.side_effect = lambda detail, stake: detail
        self.client.submit_bet.return_value = {'submitted': True, 'status': 'accepted', 'order_no': 'ORDER-1'}
        self.factory = MagicMock(return_value=self.client)
        self.executor = self.make_executor()

    def make_executor(self):
        executor = BettingExecutor(self.settings, lambda: self.hub, lambda: self.ledger, self.factory)
        executor.bind(self.temp.name + '/ledger')
        return executor

    def test_enabled_background_queue_submits_protocol_once_and_survives_restart(self):
        self.executor.enqueue(self.row)
        result = self.executor.flush()[0]
        self.assertEqual(result['status'], 'accepted')
        payload = self.client.submit_bet.call_args.args[0]
        self.assertEqual(payload['seriesOrders'][0]['orderDetailList'][0]['betAmount'], '2.00')
        self.assertEqual(payload['deviceType'], '3')
        self.assertEqual(payload['acceptOdds'], 2)
        self.assertTrue(self.make_executor().execute(self.row, self.pick)['duplicate'])
        self.client.submit_bet.assert_called_once()
        self.assertNotIn('requestId', json.dumps(self.executor.health()))

    def test_claim_precedes_transport_and_second_process_cannot_submit(self):
        def submit(payload):
            with closing(sqlite3.connect(self.executor.path)) as db:
                self.assertEqual(db.execute('SELECT status FROM orders').fetchone()[0], 'sending')
            second = self.make_executor().execute(self.row, self.pick)
            self.assertTrue(second['duplicate'])
            self.assertIsNone(second['submitted'])
            return {'submitted': True, 'status': 'accepted', 'order_no': 'ORDER-1'}
        self.client.submit_bet.side_effect = submit
        self.assertEqual(self.executor.execute(self.row, self.pick)['status'], 'accepted')
        self.client.submit_bet.assert_called_once()

    def test_timeout_and_unexpected_transport_failures_never_replay(self):
        for exc in (BetSubmissionUnknown('结果未知'), RuntimeError('internal secret')):
            with self.subTest(exc=type(exc).__name__):
                self.row['match_id'] = self.pick['order_detail']['matchId'] = type(exc).__name__
                self.client.submit_bet.side_effect = exc
                result = self.executor.execute(self.row, self.pick)
                self.assertEqual(result['status'], 'unknown')
                self.assertIsNone(result['submitted'])
                self.assertNotIn('internal secret', json.dumps(result))
                self.assertTrue(self.make_executor().execute(self.row, self.pick)['duplicate'])
        self.assertEqual(self.client.submit_bet.call_count, 2)

    def test_rejection_and_pending_receipt_are_distinct(self):
        self.client.submit_bet.side_effect = BetSubmissionRejected('拒单')
        result = self.executor.execute(self.row, self.pick)
        self.assertEqual(result['status'], 'rejected')
        self.assertFalse(result['submitted'])
        self.assertTrue(self.make_executor().execute(self.row, self.pick)['duplicate'])

    def test_actual_settled_hits_override_untrusted_pick_hits(self):
        self.pick['hit_count'] = 999
        self.ledger.recommendation_performance.return_value = {'hit_count': 0, 'settled_samples': 0}
        result = self.executor.execute(self.row, self.pick)
        self.assertIn('命中次数不足', result['reason'])
        self.factory.assert_not_called()

    def test_frequent_recommendations_preserve_reason_and_do_not_extend_cooldown(self):
        self.ledger.recommendation_performance.return_value = {'hit_count': 0, 'settled_samples': 0}
        with self.assertLogs('service.betting', level='WARNING') as logs:
            with patch('service.betting.time.monotonic', return_value=100):
                first = self.executor.execute(self.row, self.pick)
            self.assertIn('命中次数不足', first['reason'])
            self.ledger.recommendation_performance.return_value = {'hit_count': 4, 'settled_samples': 6}
            for moment in (110, 120, 129):
                with patch('service.betting.time.monotonic', return_value=moment):
                    deferred = self.executor.execute(self.row, self.pick)
                self.assertEqual(deferred['reason'], first['reason'])
                self.assertEqual(deferred['checked_at_ms'], first['checked_at_ms'])
                self.assertTrue(deferred['recheck_deferred'])
                self.assertEqual(deferred['retry_after_s'], 130 - moment)
                self.client.submit_bet.assert_not_called()
            with patch('service.betting.time.monotonic', return_value=130):
                recovered = self.executor.execute(self.row, self.pick)
        self.assertEqual(recovered['status'], 'accepted')
        self.assertEqual(self.ledger.recommendation_performance.call_count, 2)
        self.client.submit_bet.assert_called_once()
        self.assertEqual(len(logs.output), 1)
        self.assertIn(first['reason'], logs.output[0])

    def test_failed_recheck_starts_one_new_cooldown_and_logs_actual_cause(self):
        self.ledger.recommendation_performance.return_value = {'hit_count': 0, 'settled_samples': 0}
        with self.assertLogs('service.betting', level='WARNING') as logs:
            for moment in (100, 110, 130):
                with patch('service.betting.time.monotonic', return_value=moment):
                    result = self.executor.execute(self.row, self.pick)
                self.assertEqual(result['status'], 'blocked')
                self.assertIn('命中次数不足', result['reason'])
            with patch('service.betting.time.monotonic', return_value=131):
                deferred = self.executor.execute(self.row, self.pick)
        self.assertEqual(deferred['retry_after_s'], 29)
        self.assertEqual(self.ledger.recommendation_performance.call_count, 2)
        self.assertEqual(len(logs.output), 2)
        self.factory.assert_not_called()
        self.client.submit_bet.assert_not_called()

    def test_confidence_multiplier_uses_board_confidence(self):
        self.settings.config = replace(self.settings.config, betting_fixed_stake=20,
                                       betting_stake_mode='confidence_multiplier')
        self.pick['composite_confidence'] = 1  # ignored; derive from actual evidence
        result = self.executor.execute(self.row, self.pick)
        self.assertEqual(result['stake'], 13.0)

    def test_missing_session_and_directory_report_no_submission(self):
        self.executor.bind(None)
        self.assertIn('持久化', self.executor.execute(self.row, self.pick)['reason'])
        self.factory.assert_not_called()
        self.executor = self.make_executor()
        self.factory.return_value = None
        self.assertIn('账户会话', self.executor.execute(self.row, self.pick)['reason'])
        self.client.submit_bet.assert_not_called()

    def test_bad_database_blocks_request(self):
        Path(self.executor.path).write_text('corrupt database')
        self.assertEqual(self.executor.execute(self.row, self.pick)['status'], 'blocked')
        self.factory.assert_not_called()
        self.assertFalse(self.executor.health()['ready'])

    def test_switch_off_during_preparation_cancels_send(self):
        def prepare(detail, stake):
            self.settings.update({'betting_enabled': False}, 1, lambda *_: self.executor.configure())
            return detail
        self.client.prepare_bet.side_effect = prepare
        result = self.executor.execute(self.row, self.pick)
        self.assertEqual(result['status'], 'blocked')
        self.client.submit_bet.assert_not_called()

    def test_disable_clears_queued_orders(self):
        self.executor.enqueue(self.row)
        self.settings.update({'betting_enabled': False}, 1, lambda *_: self.executor.configure())
        self.assertEqual(self.executor.flush(), [])
        self.client.submit_bet.assert_not_called()

    def test_state_and_protocol_gates(self):
        for change in ({'finished': True}, {'suspended': True}, {'version': 11},
                       {'score_age_s': 999}, {'status': {'mmp': '999'}}):
            with self.subTest(change=change):
                before = dict(self.snapshot)
                self.snapshot.update(change)
                self.assertEqual(self.make_executor().execute(self.row, self.pick)['status'], 'blocked')
                self.snapshot = before
        self.pick['order_detail']['marketId'] = ''
        self.assertEqual(self.make_executor().execute(self.row, self.pick)['status'], 'blocked')
        self.client.submit_bet.assert_not_called()

    def test_harmless_new_snapshot_during_checks_does_not_prevent_execution(self):
        def prepare(detail, stake):
            self.snapshot['version'] += 1
            return detail
        self.client.prepare_bet.side_effect = prepare
        self.assertEqual(self.executor.execute(self.row, self.pick)['status'], 'accepted')

    def test_score_change_during_checks_blocks_execution(self):
        def prepare(detail, stake):
            self.snapshot['score'] = (2, 0)
            return detail
        self.client.prepare_bet.side_effect = prepare
        self.assertEqual(self.executor.execute(self.row, self.pick)['status'], 'blocked')
        self.client.submit_bet.assert_not_called()

    def test_individual_and_virtual_recommendations_are_blocked(self):
        for field, value in (('competition_type', 'virtual'), ('algorithm', 'poisson_market')):
            with self.subTest(field=field):
                row = {**self.row, field: value}
                self.assertEqual(self.make_executor().execute(row, self.pick)['status'], 'blocked')
        self.client.submit_bet.assert_not_called()

    def test_c105_preserves_app_ids_through_compute_and_auto_queue(self):
        hub = test_live_expert.LiveExpertTests().hub()
        ticks = parse_c105({'mid': 'm', 'time': int(time.time()*1000), 'hls2': {'2': [{
            'chpid': '2', 'hpid': '2', 'hid': 'market1', 'hv': '3.25',
            'ol': [{'oid': 'Over', 'ot': 'Over', 'ov': '1000000'},
                   {'oid': 'Under', 'ot': 'Under', 'ov': '198000'}]}]}})
        hub._record_ticks(ticks)
        svc = AnalysisService(MagicMock(), realtime=hub,
                              config=AnalysisConfig(use_llm=False, ledger_root=self.temp.name + '/ledger'))
        svc.update_settings({'betting_enabled': True, 'min_ev': 0, 'min_probability': 0,
                             'algorithms': ['poisson_market'], 'primary_algorithm': 'poisson_market'}, 1)
        with patch.object(svc.betting, 'enqueue', wraps=svc.betting.enqueue) as enqueue:
            row = svc.decide_matches(['m'])['decisions'][0]
        self.assertTrue(row['picks'])
        enqueue.assert_called_once()
        self.assertTrue(all(p['order_detail']['marketId'] for p in row['picks']))
        svc.betting.client_factory = self.factory
        svc.ledger.recommendation_performance = self.ledger.recommendation_performance
        result = svc.betting.flush()
        self.assertTrue(any(r['status'] == 'accepted' for r in result), result)


if __name__ == '__main__':
    unittest.main()
