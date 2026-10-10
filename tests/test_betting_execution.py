import json
import hashlib
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

from collector.leyu_account import (BetPreflightBudgetExpired, BetPreflightRetryable,
                                   BetSubmissionRejected, BetSubmissionUnknown)
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
                    'score': [1, 0], 'phase': '7',
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

    def test_parallel_prechecks_still_serialize_debits(self):
        active, maximum = 0, 0
        state_lock = threading.Lock()
        barrier = threading.Barrier(2)
        def prepare(detail, stake):
            barrier.wait(1)
            return detail
        def submit(payload):
            nonlocal active, maximum
            with state_lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(.02)
            with state_lock:
                active -= 1
            return {'status': 'accepted', 'submitted': True}
        self.client.prepare_bet.side_effect = prepare
        self.client.submit_bet.side_effect = submit
        second = {**self.pick, 'line': '3.5', 'order_detail': {**self.pick['order_detail'],
                  'marketValue': '3.5', 'playOptionsId': 'option2'}}
        self.row['picks'].append(second)
        self.snapshot['quotes'].append(replace(self.snapshot['quotes'][0], hv='3.5', oid='option2'))
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda p: self.executor.execute(self.row, p), self.row['picks']))
        self.assertEqual([r['status'] for r in results], ['accepted', 'accepted'])
        self.assertEqual(maximum, 1)

    def test_expired_quote_does_not_schedule_recomputation_of_old_input(self):
        self.pick.update(quote_received_at=time.monotonic()-3, quote_received_at_ms=int(time.time()*1000)-3000)
        result = self.executor.execute(self.row, self.pick)
        self.assertTrue(result['awaiting_new_quote'])
        self.assertFalse(result['retry_scheduled'])
        self.assertEqual(self.executor.health()['retry_pending'], 0)
        with patch('service.betting.time.monotonic', return_value=time.monotonic()+60):
            deferred = self.executor.execute(self.row, self.pick)
        self.assertTrue(deferred['recheck_deferred'])
        self.assertEqual(self.executor.health()['latency']['deadline_blocked'], 1)
        self.pick.update(quote_received_at=time.monotonic(), quote_received_at_ms=int(time.time()*1000))
        self.row['published_at_ms'] = time.time()*1000
        self.executor.enqueue(self.row)
        self.assertEqual(self.executor.flush()[0]['status'], 'accepted')
        self.client.submit_bet.assert_called_once()

    def test_preflight_budget_expiry_waits_for_new_quote_without_retry_timer(self):
        self.pick.update(quote_received_at=time.monotonic(), quote_received_at_ms=int(time.time()*1000))
        self.client.prepare_bet.side_effect = BetPreflightBudgetExpired('预算耗尽')
        result = self.executor.execute(self.row, self.pick)
        self.assertTrue(result['deadline_exceeded'])
        self.assertTrue(result['awaiting_new_quote'])
        self.assertFalse(result['retry_scheduled'])
        self.client.submit_bet.assert_not_called()
        # A newer result may arrive before the old preflight error is stored.
        # Direct execution must also recognize the newer input in that race.
        self.pick['quote_received_at_ms'] += 1
        self.client.prepare_bet.side_effect = lambda detail, stake: detail
        self.assertEqual(self.executor.execute(self.row, self.pick)['status'], 'accepted')

    def test_original_quote_budget_is_not_renewed_by_new_decision(self):
        self.pick['quote_received_at'] = time.monotonic()-2.1
        self.row['published_at_ms'] = time.time()*1000
        result = self.executor.execute(self.row, self.pick)
        self.assertTrue(result['deadline_exceeded'])
        self.assertFalse(result['latency_target_met'])
        self.client.submit_bet.assert_not_called()
        self.assertEqual(self.executor.health()['latency']['submissions'], 0)

    def test_restored_quote_cannot_be_given_a_new_execution_clock(self):
        self.pick['quote_received_at'] = None
        result = self.executor.execute(self.row, self.pick)
        self.assertEqual(result['status'], 'blocked')
        self.client.submit_bet.assert_not_called()

    def test_durable_replay_is_required_before_claim_or_submit(self):
        self.executor.on_persisted = MagicMock(return_value=False)
        result = self.executor.execute(self.row, self.pick)
        self.assertTrue(result['deadline_exceeded'])
        self.client.submit_bet.assert_not_called()
        with closing(sqlite3.connect(self.executor.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM orders').fetchone()[0], 0)

    def test_success_records_submit_and_receipt_from_original_quote(self):
        self.pick['quote_received_at'] = time.monotonic()-.1
        result = self.executor.execute(self.row, self.pick)
        self.assertEqual(result['status'], 'accepted')
        self.assertTrue(result['latency_target_met'])
        self.assertGreaterEqual(result['quote_to_submit_ms'], 100)
        self.assertGreaterEqual(result['quote_to_receipt_ms'], result['quote_to_submit_ms'])
        self.assertIn('venue_preflight', result['timings_ms'])

    def test_deadline_client_receives_same_absolute_budget_in_both_calls(self):
        self.pick['quote_received_at'] = time.monotonic()
        deadlines = []
        self.client.supports_deadline = True
        def prepare(detail, stake, *, deadline):
            deadlines.append(deadline)
            return detail
        def submit(payload, *, deadline):
            deadlines.append(deadline)
            self.client.last_submission_sent_at = time.monotonic()
            return {'status': 'accepted', 'submitted': True}
        self.client.prepare_bet.side_effect = prepare
        self.client.submit_bet.side_effect = submit
        result = self.executor.execute(self.row, self.pick)
        self.assertEqual(result['status'], 'accepted')
        self.assertLessEqual(deadlines[0], deadlines[1])
        self.assertAlmostEqual(deadlines[1], self.pick['quote_received_at']+2)

    def refresh_recommendation(self, odds=2.05, **selection):
        self.pick.update(odds=odds, **selection)
        self.pick['order_detail'].update(oddFinally=str(odds), marketValue=self.pick['line'],
                                         playOptions=self.pick['outcome'])
        self.row['version'] += 1
        self.row['published_at_ms'] = time.time() * 1000
        self.snapshot.update(version=self.row['version'], quotes=[LiveQuote(
            self.row['match_id'], '2', self.pick['line'], 'option1', self.pick['outcome'],
            odds, int(time.time() * 1000))])

    def test_odds_changes_and_restart_preserve_one_order(self):
        first = self.executor.execute(self.row, self.pick)
        self.assertEqual(first['status'], 'accepted')
        for executor, odds in ((self.executor, 2.05), (self.make_executor(), 1.85),
                               (self.make_executor(), 1.95)):
            with self.subTest(odds=odds):
                self.refresh_recommendation(odds)
                executor.enqueue(self.row)
                duplicate = executor.flush()[0]
                self.assertTrue(duplicate.get('duplicate'), duplicate)
                self.assertEqual(duplicate['identity'], first['identity'])
                self.assertEqual(duplicate['order_no'], 'ORDER-1')
        self.client.submit_bet.assert_called_once()
        self.client.prepare_bet.assert_called_once()

    def test_changed_odds_never_replay_rejected_pending_or_unknown_orders(self):
        for receipt in (BetSubmissionRejected('拒单'), BetSubmissionUnknown('未知'),
                        RuntimeError('transport failure'),
                        {'submitted': True, 'status': 'pending', 'order_no': 'ORDER-1'}):
            with self.subTest(receipt=receipt):
                self.row['match_id'] = self.pick['order_detail']['matchId'] = str(receipt)
                self.client.submit_bet.side_effect = receipt if isinstance(receipt, Exception) else None
                if isinstance(receipt, dict):
                    self.client.submit_bet.return_value = receipt
                self.refresh_recommendation(1.95)
                first = self.executor.execute(self.row, self.pick)
                self.assertIn(first['status'], ('rejected', 'pending', 'unknown'))
                self.assertEqual(self.executor.health()['retry_pending'], 0)
                self.refresh_recommendation(2.05)
                duplicate = self.make_executor().execute(self.row, self.pick)
                self.assertTrue(duplicate.get('duplicate'), duplicate)
                self.assertEqual(duplicate['status'], first['status'])
        self.assertEqual(self.client.submit_bet.call_count, 4)

    def test_changed_odds_during_transport_cannot_submit_second_order(self):
        def submit(payload):
            self.refresh_recommendation()
            duplicate = self.make_executor().execute(self.row, self.pick)
            self.assertTrue(duplicate.get('duplicate'), duplicate)
            self.assertEqual(duplicate['status'], 'unknown')
            self.assertIsNone(duplicate['submitted'])
            return {'submitted': True, 'status': 'accepted', 'order_no': 'ORDER-1'}
        self.client.submit_bet.side_effect = submit
        self.assertEqual(self.executor.execute(self.row, self.pick)['status'], 'accepted')
        self.client.submit_bet.assert_called_once()

    def test_concurrent_changed_odds_claim_only_one_order(self):
        barrier = threading.Barrier(2)
        def prepare(detail, stake):
            barrier.wait(timeout=5)
            return detail
        self.client.prepare_bet.side_effect = prepare
        workers = []
        for odds in (1.95, 2.05):
            pick = {**self.pick, 'odds': odds, 'order_detail': {**self.pick['order_detail'], 'oddFinally': str(odds)}}
            row = {**self.row, 'picks': [pick]}
            snapshot = {**self.snapshot, 'quotes': [replace(self.snapshot['quotes'][0], odds=odds)]}
            hub = MagicMock()
            hub.decision_snapshot.return_value = snapshot
            executor = BettingExecutor(RuntimeSettings(self.settings.config), lambda h=hub: h,
                                       lambda: self.ledger, self.factory)
            executor.bind(self.temp.name + '/ledger')
            workers.append((executor, row, pick))
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(executor.execute, row, pick) for executor, row, pick in workers]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sum(r['status'] == 'accepted' and not r.get('duplicate') for r in results), 1, results)
        self.assertEqual(sum(bool(r.get('duplicate')) or r['status'] == 'duplicate' for r in results), 1, results)
        self.client.submit_bet.assert_called_once()

    def test_legacy_price_keys_survive_upgrade_with_receipts_intact(self):
        legacy = []
        with closing(sqlite3.connect(self.executor.path)) as db:
            db.execute('CREATE TABLE orders (identity TEXT PRIMARY KEY, at REAL NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL)')
            for index, status in enumerate(('accepted', 'pending', 'sending', 'unknown', 'rejected')):
                mid = 'legacy-' + status
                for odds in (1.95, 1.85):
                    # Exact old format; payloads did not retain the account or odds.
                    identity = hashlib.sha256(json.dumps(['old-account', mid, 'OU', '2.5', 'over', str(odds)],
                                                         ensure_ascii=False).encode()).hexdigest()
                    payload = {'identity': identity, 'match_id': mid, 'market': 'OU', 'line': '2.5',
                               'outcome': 'over', 'status': status, 'submitted': status in ('accepted', 'pending'),
                               'order_no': identity, 'checked_at_ms': index}
                    raw = json.dumps(payload, ensure_ascii=False)
                    db.execute('INSERT INTO orders VALUES (?,?,?,?)', (identity, index, status, raw))
                    legacy.append((identity, status, raw))
            db.commit()
        for status in ('accepted', 'pending', 'sending', 'unknown', 'rejected'):
            with self.subTest(status=status), patch.dict('os.environ', {'LEYU_APP_LOGIN_NAME': 'current-account'}):
                self.row['match_id'] = self.pick['order_detail']['matchId'] = 'legacy-' + status
                self.refresh_recommendation()
                for executor in (self.executor, self.make_executor()):
                    duplicate = executor.execute(self.row, self.pick)
                    self.assertTrue(duplicate.get('duplicate'), duplicate)
                    self.assertEqual(duplicate['status'], 'unknown' if status == 'sending' else status)
                    self.assertEqual(duplicate['order_no'], duplicate['identity'])
        with closing(sqlite3.connect(self.executor.path)) as db:
            saved = db.execute('SELECT identity,status,payload FROM orders ORDER BY identity').fetchall()
        self.assertEqual(saved, sorted(legacy))
        self.factory.assert_not_called()
        self.client.submit_bet.assert_not_called()

    def test_distinct_lines_directions_and_accounts_remain_separate(self):
        with patch.dict('os.environ', {'LEYU_APP_LOGIN_NAME': 'account-a'}):
            self.assertEqual(self.executor.execute(self.row, self.pick)['status'], 'accepted')
            self.refresh_recommendation(line='3.5')
            self.assertEqual(self.executor.execute(self.row, self.pick)['status'], 'accepted')
            self.refresh_recommendation(outcome='under')
            self.assertEqual(self.executor.execute(self.row, self.pick)['status'], 'accepted')
            self.refresh_recommendation(outcome='over')
            self.assertTrue(self.executor.execute(self.row, self.pick).get('duplicate'))
            self.refresh_recommendation(market='OU_1H')
            self.assertEqual(self.executor.execute(self.row, self.pick)['status'], 'accepted')
            self.row['match_id'] = self.pick['order_detail']['matchId'] = 'another-match'
            self.refresh_recommendation()
            self.assertEqual(self.executor.execute(self.row, self.pick)['status'], 'accepted')
        with patch.dict('os.environ', {'LEYU_APP_LOGIN_NAME': ' ACCOUNT-A '}):
            self.assertTrue(self.make_executor().execute(self.row, self.pick).get('duplicate'))
        with patch.dict('os.environ', {'LEYU_APP_LOGIN_NAME': 'account-b'}):
            self.assertEqual(self.make_executor().execute(self.row, self.pick)['status'], 'accepted')
        self.assertEqual(self.client.submit_bet.call_count, 6)

    def test_invalid_legacy_payload_blocks_send_and_rolls_back_migration(self):
        with closing(sqlite3.connect(self.executor.path)) as db:
            db.execute('CREATE TABLE orders (identity TEXT PRIMARY KEY, at REAL NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('INSERT INTO orders VALUES (?,?,?,?)', ('legacy', 1, 'sending', '{}'))
            db.commit()
        with self.assertLogs('service.betting', level='WARNING'):
            result = self.executor.execute(self.row, self.pick)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('历史订单标识不完整', result['reason'])
        with closing(sqlite3.connect(self.executor.path)) as db:
            self.assertEqual(db.execute('SELECT * FROM orders').fetchall(), [('legacy', 1, 'sending', '{}')])
            self.assertEqual([col[1] for col in db.execute('PRAGMA table_info(orders)')],
                             ['identity', 'at', 'status', 'payload'])
        self.factory.assert_not_called()
        self.client.submit_bet.assert_not_called()

    def test_claim_failure_rolls_back_without_sending_or_reserving_order(self):
        self.settings.claim_bet = MagicMock(side_effect=RuntimeError('persistence failure'))
        with self.assertLogs('service.betting', level='WARNING'):
            result = self.executor.execute(self.row, self.pick)
        self.assertEqual(result['status'], 'blocked')
        with closing(sqlite3.connect(self.executor.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 0)
        self.client.submit_bet.assert_not_called()
        del self.settings.claim_bet
        self.assertEqual(self.make_executor().execute(self.row, self.pick)['status'], 'accepted')

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

    def test_automatic_submission_includes_live_chinese_match_metadata(self):
        self.row.update(home='旧主队名', away='旧客队名', league='旧联赛')
        self.snapshot['info'] = {'home': '当前主队', 'away': '当前客队', 'league': '当前中文联赛'}
        self.executor.enqueue(self.row)
        self.assertEqual(self.executor.flush()[0]['status'], 'accepted')
        detail = self.client.submit_bet.call_args.args[0]['seriesOrders'][0]['orderDetailList'][0]
        self.assertEqual(detail['matchInfo'], '当前主队 v 当前客队')
        self.assertEqual(detail['matchName'], '当前中文联赛')
        self.assertEqual(detail['playName'], '全场大小')
        self.assertEqual(detail['playOptionName'], '全场进球数>2.5')
        self.assertEqual(detail['sportName'], '足球')
        self.assertEqual(detail['playOptions'], 'Over')
        self.assertEqual(detail['playOptionsId'], 'option1')

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
        for change in ({'finished': True}, {'suspended': True}, {'score': (2, 0)},
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

    def test_unchanged_c105_version_before_checks_does_not_block_bet(self):
        self.snapshot['version'] = 999
        result = self.executor.execute(self.row, self.pick)
        self.assertEqual(result['status'], 'accepted', result)
        self.client.submit_bet.assert_called_once()

    def test_new_decision_bypasses_quote_failure_cooldown_but_never_repeats_order(self):
        self.executor.on_recompute = MagicMock()
        self.snapshot['quotes'][0].odds = 2.05
        with self.assertLogs('service.betting', level='WARNING'), patch('service.betting.time.monotonic', return_value=100):
            blocked = self.executor.execute(self.row, self.pick)
        self.assertEqual(blocked['stage'], 'live_quote')
        self.assertTrue(blocked['retry_on_new_decision'])
        self.executor.on_recompute.assert_not_called()
        self.client.prepare_bet.assert_not_called()
        self.refresh_recommendation(2.05)
        with patch('service.betting.time.monotonic', return_value=101):
            self.assertTrue(self.executor.execute(self.row, self.pick)['recheck_deferred'])
        with patch('service.betting.time.monotonic', return_value=102):
            self.executor.flush()
            result = self.executor.execute(self.row, self.pick)
        self.executor.on_recompute.assert_called_once_with(['m'])
        self.assertEqual(result['status'], 'accepted', result)
        self.refresh_recommendation(2.15)
        self.assertTrue(self.executor.execute(self.row, self.pick)['duplicate'])
        self.client.submit_bet.assert_called_once()

    def test_preflight_timeout_retries_fresh_decisions_with_bounded_exponential_backoff(self):
        self.settings.config = replace(self.settings.config, betting_retry_max_attempts=3)
        self.executor.on_recompute = MagicMock()
        self.client.prepare_bet.side_effect = BetPreflightRetryable('读取超时')
        for index, moment, delay in ((1, 100, 2), (2, 102, 4), (3, 106, 30)):
            self.row['published_at_ms'] += 1
            with patch('service.betting.time.monotonic', return_value=moment), self.assertLogs('service.betting'):
                result = self.executor.execute(self.row, self.pick)
            self.assertEqual(result['attempt'], index)
            self.assertEqual(result['retry_after_s'], delay)
        self.assertTrue(result['retry_exhausted'])
        self.assertFalse(result['retry_scheduled'])
        self.assertEqual(self.executor.health()['retry_pending'], 0)
        self.client.submit_bet.assert_not_called()
        with patch('service.betting.time.monotonic', return_value=107):
            self.assertTrue(self.executor.execute(self.row, self.pick)['recheck_deferred'])
        self.client.prepare_bet.side_effect = lambda d, s: d
        self.row['published_at_ms'] += 1
        with patch('service.betting.time.monotonic', return_value=136):
            accepted = self.executor.execute(self.row, self.pick)
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual(accepted['attempt'], 1)
        self.client.submit_bet.assert_called_once()

    def test_retry_timer_requests_recomputation_without_replaying_old_row(self):
        self.executor.on_recompute = MagicMock()
        self.client.prepare_bet.side_effect = BetPreflightRetryable('读取失败')
        with patch('service.betting.time.monotonic', return_value=100), self.assertLogs('service.betting'):
            self.executor.execute(self.row, self.pick)
        with patch('service.betting.time.monotonic', return_value=102):
            self.assertEqual(self.executor.flush(), [])
            self.assertTrue(self.executor.execute(self.row, self.pick)['awaiting_new_decision'])
            self.executor.flush()
        self.executor.on_recompute.assert_called_once_with(['m'])
        self.client.prepare_bet.assert_called_once()
        self.client.submit_bet.assert_not_called()

    def test_observe_decision_cancels_pending_retry_and_queued_pick(self):
        self.client.prepare_bet.side_effect = BetPreflightRetryable('读取失败')
        self.executor.enqueue(self.row)
        with self.assertLogs('service.betting'):
            self.executor.flush()
        self.assertTrue(self.executor.health()['last_result']['retry_scheduled'])
        self.executor.enqueue(self.row)
        self.executor.enqueue({**self.row, 'published_at_ms': self.row['published_at_ms'] + 1, 'picks': []})
        health = self.executor.health()
        self.assertEqual(health['retry_pending'], 0)
        self.assertTrue(health['last_result']['retry_cancelled'])
        self.assertFalse(health['last_result']['retry_scheduled'])
        self.assertFalse(health['last_result']['awaiting_new_decision'])
        self.assertEqual(self.executor.flush(), [])
        self.client.submit_bet.assert_not_called()

    def test_disable_cancels_retry_timers(self):
        self.client.prepare_bet.side_effect = BetPreflightRetryable('读取失败')
        self.executor.enqueue(self.row)
        with self.assertLogs('service.betting'):
            self.executor.flush()
        self.settings.update({'betting_enabled': False}, 1, lambda *_: self.executor.configure())
        health = self.executor.health()
        self.assertEqual(health['retry_pending'], 0)
        self.assertTrue(health['last_result']['retry_cancelled'])
        self.assertFalse(health['last_result']['retry_scheduled'])
        self.assertEqual(self.executor.flush(), [])
        self.client.submit_bet.assert_not_called()

    def test_stop_clears_retry_queue_and_reports_cancellation(self):
        self.client.prepare_bet.side_effect = BetPreflightRetryable('读取失败')
        self.executor.enqueue(self.row)
        with self.assertLogs('service.betting'):
            self.executor.flush()
        self.executor.enqueue(self.row)
        self.executor.stop()
        health = self.executor.health()
        self.assertEqual(health['queued'], 0)
        self.assertEqual(health['retry_pending'], 0)
        self.assertTrue(health['last_result']['retry_cancelled'])
        self.assertFalse(health['last_result']['retry_scheduled'])
        self.assertEqual(self.executor.flush(), [])
        self.client.submit_bet.assert_not_called()

    def test_stop_preserves_accepted_receipt_without_marking_it_cancelled(self):
        self.executor.enqueue(self.row)
        self.assertEqual(self.executor.flush()[0]['status'], 'accepted')
        self.executor.stop()
        last = self.executor.health()['last_result']
        self.assertEqual(last['status'], 'accepted')
        self.assertFalse(last.get('retry_cancelled'))
        self.assertEqual(last['order_no'], 'ORDER-1')
        self.client.submit_bet.assert_called_once()

    def test_slow_first_order_does_not_freeze_following_decision_batch(self):
        other = {**self.row, 'match_id': 'other'}
        newest = {**other, 'published_at_ms': other['published_at_ms'] + 1000}
        self.executor.enqueue(self.row)
        self.executor.enqueue(other)
        seen = []
        def execute(row, pick):
            seen.append(row)
            if row['match_id'] == 'm':
                self.executor.enqueue(newest)
            return {'status': 'accepted'}
        with patch.object(self.executor, 'execute', side_effect=execute):
            self.executor.flush()
        self.assertEqual([r['match_id'] for r in seen], ['m', 'other'])
        self.assertEqual(seen[1]['published_at_ms'], newest['published_at_ms'])

    def test_enqueue_wakes_background_executor_and_submits_fake_order_once(self):
        submitted = threading.Event()
        def submit(payload):
            submitted.set()
            return {'submitted': True, 'status': 'accepted', 'order_no': 'TEST'}
        self.client.submit_bet.side_effect = submit
        self.executor.start()
        self.addCleanup(self.executor.stop)
        self.executor.enqueue(self.row)
        self.assertTrue(submitted.wait(2))
        self.executor.stop()
        self.client.submit_bet.assert_called_once()

    def test_new_identical_decision_during_preflight_renews_decision_validity(self):
        self.executor.enqueue(self.row)
        def prepare(detail, stake):
            self.executor.enqueue({**self.row, 'published_at_ms': self.row['published_at_ms'] + 1})
            self.row['published_at_ms'] = (time.time() - 16) * 1000
            return detail
        self.client.prepare_bet.side_effect = prepare
        result = self.executor.execute(self.row, self.pick)
        self.assertEqual(result['status'], 'accepted', result)
        self.client.submit_bet.assert_called_once()

    def test_withdrawn_recommendation_during_preflight_never_submits(self):
        self.executor.enqueue(self.row)
        def prepare(detail, stake):
            self.executor.enqueue({**self.row, 'picks': [], 'published_at_ms': self.row['published_at_ms'] + 1})
            return detail
        self.client.prepare_bet.side_effect = prepare
        with self.assertLogs('service.betting'):
            result = self.executor.execute(self.row, self.pick)
        self.assertIn('撤回', result['reason'])
        self.assertTrue(result['retry_cancelled'])
        self.assertEqual(self.executor.health()['retry_pending'], 0)
        self.client.submit_bet.assert_not_called()

    def test_preflight_failures_report_specific_final_cause(self):
        for mutation, reason in (
            (lambda: setattr(self.snapshot['quotes'][0], 'odds', 2.05), '赔率已变化'),
            (lambda: self.snapshot.update(quotes=[]), '盘口或选项已更新'),
            (lambda: self.snapshot.update(score=(2, 0)), '比分或比赛阶段已变化'),
            (lambda: self.row.update(published_at_ms=(time.time()-16)*1000), '综合推荐已过期'),
            (lambda: self.snapshot.update(score_age_s=999), '比赛时钟已过期'),
        ):
            with self.subTest(reason=reason):
                self.setUp()
                def prepare(detail, stake, mutate=mutation):
                    mutate()
                    return detail
                self.client.prepare_bet.side_effect = prepare
                with self.assertLogs('service.betting', level='WARNING'):
                    result = self.executor.execute(self.row, self.pick)
                self.assertEqual(result['stage'], 'final_quote', result)
                self.assertIn(reason, result['reason'])
                self.client.submit_bet.assert_not_called()

    def test_cached_account_client_reuses_venue_session_and_invalidates_credentials(self):
        self.executor.client_factory = None
        factory = MagicMock(return_value=self.client)
        with patch.dict('os.environ', {'LEYU_APP_LOGIN_NAME': 'account-a'}):
            self.assertIs(self.executor._account_client(factory), self.client)
            self.assertIs(self.executor._account_client(factory), self.client)
        factory.assert_called_once()
        with patch.dict('os.environ', {'LEYU_APP_LOGIN_NAME': 'account-b'}):
            self.executor._account_client(factory)
        self.assertEqual(factory.call_count, 2)

    def test_status_for_each_selection_preserves_receipt_over_last_blocked_result(self):
        self.assertEqual(self.executor.execute(self.row, self.pick)['status'], 'accepted')
        self.refresh_recommendation(line='3.5')
        self.ledger.recommendation_performance.return_value = {'hit_count':0,'settled_samples':0}
        with self.assertLogs('service.betting', level='WARNING'):
            self.executor.execute(self.row, self.pick)
        keys = [('m','OU','2.5','over'),('m','OU','3.5','over'),('other','OU','2.5','over')]
        state = self.executor.statuses(keys)
        self.assertEqual(state[keys[0]]['order_no'], 'ORDER-1')
        self.assertEqual(state[keys[1]]['status'], 'blocked')
        self.assertIn('命中次数不足', state[keys[1]]['reason'])
        self.assertEqual(state[keys[2]]['status'], 'not_submitted')

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
        # This case owns flush() explicitly. The background wake-up is covered
        # separately with its fake client installed before enqueue.
        with patch.object(BettingExecutor, 'start'):
            svc = AnalysisService(MagicMock(), realtime=hub,
                                  config=AnalysisConfig(use_llm=False, ledger_root=self.temp.name + '/ledger'))
        with patch.object(svc.betting, 'start'):
            svc.update_settings({'betting_enabled': True, 'min_ev': 0, 'min_probability': 0,
                                 'algorithms': ['poisson_market'], 'primary_algorithm': 'poisson_market'}, 1)
        # Simulate start's lifecycle transition while keeping manual flush
        # ownership. Construction with disabled betting called stop().
        svc.betting._stop.clear()
        with patch.object(svc.betting, 'enqueue', wraps=svc.betting.enqueue) as enqueue:
            row = svc.decide_matches(['m'])['decisions'][0]
        self.assertTrue(row['picks'])
        enqueue.assert_called_once()
        self.assertTrue(all(p['order_detail']['marketId'] for p in row['picks']))
        svc.betting.client_factory = self.factory
        svc.ledger.recommendation_performance = self.ledger.recommendation_performance
        svc.live_expert.flush()  # Full decision must be durable before fake debit.
        result = svc.betting.flush()
        self.assertTrue(any(r['status'] == 'accepted' for r in result), result)


if __name__ == '__main__':
    unittest.main()
