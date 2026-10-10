"""Permanent archive contracts, retries and the boundary of event modelling."""
import gzip
import json
import signal
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

from collector.leyu_client import parse_odds_block
from collector.leyu_realtime import PriceTick, RealtimeHub, ScoreStore, TrendStore, parse_c103
from service.analysis import AnalysisConfig, AnalysisService
from service.ledger import DecisionLedger, LedgerEntry
from service.live_expert import LiveExpertService
from service.live_review import LiveReview
from service.runtime_settings import RuntimeConfig
from store.history import HistoryJournal, encode_checkpoint
from tests import test_live_expert


def read_history(path):
    with gzip.open(path, 'rt', encoding='utf-8') as file:
        return [json.loads(line) for line in file]


class CheckpointCodecTests(unittest.TestCase):
    def test_checkpoint_preserves_native_fields_with_installed_codec(self):
        payload = {'version':1, 'rows':[['比赛甲','2','2.5','123456789012345678',
                    'Over',1.951234,1791656800123,{'marketValue':'2.5','oddFinally':'1.951234'}]]}
        encoded = encode_checkpoint(payload)
        self.assertIsInstance(encoded, bytes)
        self.assertEqual(json.loads(encoded), payload)

    def test_missing_optional_codec_uses_readable_standard_json(self):
        payload = {'rows':[['比赛',2.0,123]], 'version':1}
        with patch('store.history.importlib.import_module', side_effect=ModuleNotFoundError):
            encoded = encode_checkpoint(payload)
        self.assertEqual(json.loads(encoded), payload)

    def test_encoder_failure_preserves_previous_atomic_checkpoint(self):
        from collector.leyu_realtime import LiveBook
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'live.json'
            book = LiveBook(str(path))
            book.upsert_many([PriceTick('m','2','','','over','Over',2,2,int(time.time()*1000))])
            self.assertTrue(book.save(force=True))
            old = path.read_bytes()
            with patch('collector.leyu_realtime.encode_checkpoint', side_effect=TypeError('invalid')):
                self.assertFalse(book.save(force=True))
            self.assertEqual(path.read_bytes(), old)
            self.assertTrue(book.last_error)


class TrendResumeTests(unittest.TestCase):
    def test_startup_queues_history_without_reading_archive(self):
        with tempfile.TemporaryDirectory() as root:
            hub = RealtimeHub(MagicMock(), trend_root=str(Path(root) / '_trends'))
            with patch.object(hub.live, 'live_mids', return_value=['m']), \
                    patch.object(hub.trend_store, 'load') as load:
                hub._resume_from_store()
            load.assert_not_called()
            self.assertIn('m', hub._resume_pending)
            with patch.object(hub.trend_store, 'load', return_value=[]):
                self.assertFalse(hub.trend('m')['history_loading'])
            self.assertNotIn('m', hub._resume_pending)

    def test_slow_history_load_does_not_block_quotes_or_reads_and_preserves_live_tail(self):
        with tempfile.TemporaryDirectory() as root:
            hub = RealtimeHub(MagicMock(), trend_root=str(Path(root) / '_trends'))
            now = int(time.time() * 1000)
            old = PriceTick('m', '2', 'h', '2.5', 'o', 'Over', 1.8, 1.9, now - 2000)
            baseline = replace(old, old_ov=1.95, new_ov=1.95, ts_ms=now)
            latest = replace(old, old_ov=1.95, new_ov=2.05, ts_ms=now + 1)
            entered, release = threading.Event(), threading.Event()
            def load(*_a, **_kw):
                entered.set()
                release.wait(5)
                return [old.as_dict(), latest.as_dict()]
            worker = threading.Thread(target=hub._resume_loop)
            publisher = threading.Thread(target=lambda: hub._record_ticks([latest]))
            reader = threading.Thread(target=lambda: hub.trend('m'))
            with patch.object(RealtimeHub, 'running', new_callable=PropertyMock, return_value=True), \
                    patch.object(hub.trend_store, 'load', side_effect=load) as loader:
                try:
                    hub._record_ticks([baseline])
                    worker.start()
                    self.assertTrue(entered.wait(2))
                    publisher.start()
                    publisher.join(1)
                    self.assertFalse(publisher.is_alive(), 'history IO blocks live quote writer')
                    reader.start()
                    reader.join(1)
                    self.assertFalse(reader.is_alive(), 'history IO blocks trend query')
                    self.assertTrue(hub.trend('m')['history_loading'])
                    self.assertEqual(hub.decision_snapshot('m')['quotes'][0].odds, 2.05)
                finally:
                    release.set()
                    hub._stop.set()
                    hub._resume_wake.set()
                    worker.join(3)
                    if publisher.ident is not None:
                        publisher.join(3)
                    if reader.ident is not None:
                        reader.join(3)
                loader.assert_called_once()
            summary = hub.trend('m', '2', '2.5')
            self.assertEqual(summary['n'], 2)
            self.assertFalse(summary['history_loading'])
            self.assertEqual(summary['last']['new'], 2.05)
            self.assertEqual(hub._last_price[('m', '2', '2.5', 'o')], 2.05)

    def test_failed_resume_releases_single_flight_and_can_retry(self):
        with tempfile.TemporaryDirectory() as root:
            hub = RealtimeHub(MagicMock(), trend_root=str(Path(root) / '_trends'))
            old = PriceTick('m', '2', 'h', '2.5', 'o', 'Over', 1.8, 1.9, 1000)
            with patch.object(hub.trend_store, 'load', side_effect=[OSError('offline'), [old.as_dict()]]):
                with self.assertRaises(OSError):
                    hub._resume_match('m')
                self.assertNotIn('m', hub._resuming_mids)
                self.assertNotIn('m', hub._resumed_mids)
                hub._resume_match('m')
            self.assertIn('m', hub._resumed_mids)
            self.assertEqual(hub.trend('m', '2', '2.5')['n'], 1)


class HistoryJournalTests(unittest.TestCase):
    def test_multiple_members_and_restart_preserve_old_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'nested' / 'history.jsonl.gz'
            journal = HistoryJournal(path)
            self.assertEqual(journal.append([{'score': [0, 0], 'event': '角球'}]), 1)
            original = path.read_bytes()
            HistoryJournal(path).append([{'score': [1, 0]}, {'score': [1, 1]}])
            self.assertTrue(path.read_bytes().startswith(original))
            self.assertEqual([r['score'] for r in read_history(path)], [[0, 0], [1, 0], [1, 1]])
            self.assertEqual(journal.health()['retention'], 'permanent')
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_failed_fsync_rolls_back_attempt_and_can_retry(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'history.jsonl.gz'
            journal = HistoryJournal(path)
            journal.append([{'n': 0}])
            original = path.read_bytes()
            with patch('store.history.os.fsync', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    journal.append([{'n': 1}])
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(journal.last_error)
            journal.append([{'n': 1}])
            self.assertEqual(read_history(path), [{'n': 0}, {'n': 1}])
            self.assertEqual(journal.last_error, '')

    def test_invalid_json_does_not_damage_existing_history(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'history.jsonl.gz'
            journal = HistoryJournal(path)
            journal.append([{'n': 0}])
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                journal.append([{'n': 1}, {'n': float('nan')}])
            self.assertEqual(path.read_bytes(), original)
            self.assertIn('ValueError', journal.health()['last_error'])

    def test_partial_write_preserves_the_previous_valid_member(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'history.jsonl.gz'
            journal = HistoryJournal(path)
            journal.append([{'n': 0}])
            original = path.read_bytes()
            file = path.open('a+b', buffering=0)
            partial = MagicMock(wraps=file)
            partial.__enter__.return_value = partial
            partial.__exit__.side_effect = lambda *args: file.close()
            partial.write.side_effect = lambda data: file.write(data[:len(data)//2])
            with patch.object(Path, 'open', return_value=partial):
                with self.assertRaisesRegex(OSError, 'incomplete'):
                    journal.append([{'n': 1}])
            self.assertEqual(path.read_bytes(), original)
            journal.append([{'n': 1}])
            self.assertEqual(read_history(path), [{'n': 0}, {'n': 1}])


class TrendHistoryTests(unittest.TestCase):
    def tick(self, price=2.0, ts=None):
        return PriceTick('m', '2', 'market', '2.5', 'over', 'Over', price, price,
                         ts if ts is not None else int(time.time() * 1000))

    def test_old_capacity_limits_never_rotate_or_remove_history(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'm.jsonl'
            # Sparse padding crosses the old 4 MiB/256 MiB caps without filling disk.
            for name, size in [('m.jsonl', 5 * 1024**2), ('old.jsonl', 257 * 1024**2)]:
                with (Path(root) / name).open('wb') as file:
                    file.write(b'old-prefix\n')
                    file.truncate(size)
            generation = Path(root) / 'm.jsonl.1'
            generation.write_bytes(b'previous-generation\n')
            old_size = path.stat().st_size
            store = TrendStore(root)
            store.append_many([self.tick()])
            self.assertGreater(path.stat().st_size, old_size)
            with path.open('rb') as file:
                self.assertEqual(file.read(11), b'old-prefix\n')
            self.assertEqual(generation.read_bytes(), b'previous-generation\n')
            self.assertTrue((Path(root) / 'old.jsonl').exists())
            self.assertEqual(store.health()['rotated'], 0)
            self.assertEqual(store.health()['pruned'], 0)

    def test_retry_does_not_drop_a_tick_and_tail_load_is_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            store = TrendStore(root)
            store.append_many([self.tick(2.0)])
            before = (Path(root) / 'm.jsonl').read_bytes()
            with patch('collector.leyu_realtime.os.fsync', side_effect=OSError('disk full')):
                store.append_many([self.tick(2.1)])
            self.assertEqual((Path(root) / 'm.jsonl').read_bytes(), before)
            self.assertEqual(store.health()['pending'], 1)
            store.append_many([self.tick(2.2)])
            self.assertEqual([r['new'] for r in store.load('m', 2)], [2.1, 2.2])
            self.assertEqual(store.health()['pending'], 0)
            self.assertEqual(store.health()['dropped'], 0)

    def test_baseline_changes_and_rest_quotes_are_archived(self):
        with tempfile.TemporaryDirectory() as root:
            hub = RealtimeHub(MagicMock(), trend_root=str(Path(root) / '_trends'), resume=False)
            hub._record_ticks([self.tick(2.0)])
            hub._record_ticks([self.tick(2.0)])
            hub._record_ticks([self.tick(2.1)])
            rows = hub.trend_store.load('m')
            self.assertEqual([(r['old'], r['new']) for r in rows], [(2.0, 2.0), (2.0, 2.1)])
            self.assertTrue(all(r['received_at_ms'] for r in rows))
            self.assertEqual(rows[0]['hid'], 'market')
            match = parse_odds_block({'data': [{'mid': 'rest', 'ms': 1, 'mst': '12', 'mmp': '1',
                'msc': 'S1|0:0,S555|1:2', 'hpsData': [{'hps': [{'chpid': '2', 'hpid': '2', 'hl': [
                    {'hid': 'h', 'hv': '2.5', 'ol': [{'oid': 'o', 'ot': 'Over', 'ov': '200000',
                                                    'ctsp': str(int(time.time()*1000))}]}]}]}]}]})[0]
            hub.seed_matches([match])
            self.assertTrue(hub.trend_store.load('rest'))
            self.assertTrue(read_history(Path(root) / '_live/events.jsonl.gz')[-1]['quotes'])
            restored = RealtimeHub(MagicMock(), trend_root=str(Path(root) / '_trends'))
            restored._record_ticks([self.tick(2.2)])
            self.assertEqual(restored.trend_store.load('m')[-1]['old'], 2.1)


class ScoreAndEventHistoryTests(unittest.TestCase):
    def test_per_match_score_staging_preserves_restored_matches_and_all_revisions(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'scores.json'
            initial = ScoreStore(str(path))
            initial.save({'old': (2, 1)}, ['old'], force=True,
                         final_proofs={'old': {'mmp': '999'}})
            store = ScoreStore(str(path))
            store.load()
            store.stage('m', (0, 0), False, None, None)
            store.stage('m', (1, 0), False, (0, 0), None)
            store.stage('m', (1, 0), True, (0, 0), {'mmp': '999'})
            self.assertEqual(store.history_health()['pending'], 3)
            self.assertTrue(store.flush(force=True))
            restored = ScoreStore(str(path)).load()
            self.assertEqual(restored['old']['ft'], [2, 1])
            self.assertTrue(restored['old']['done'])
            self.assertTrue(restored['m']['done'])
            self.assertEqual(restored['m']['ht'], [0, 0])
            self.assertEqual([r['ft'] for r in read_history(store.history.path) if r['match_id'] == 'm'],
                             [[0, 0], [1, 0], [1, 0]])

    def test_deferred_scores_keep_every_revision_and_retry_failed_flush(self):
        with tempfile.TemporaryDirectory() as root:
            store = ScoreStore(str(Path(root) / 'scores.json'))
            for score in ((0, 0), (1, 0), (1, 1)):
                store.save({'m': score}, [], defer=True)
            self.assertFalse(store.path.exists())
            self.assertEqual(store.history_health()['pending'], 3)
            with patch.object(store.history, 'append', side_effect=OSError('disk full')):
                self.assertFalse(store.flush(force=True))
            self.assertEqual(store.history_health()['pending'], 3)
            store.save({'m': (1, 1)}, ['m'], final_proofs={'m': {'mmp': '999'}}, defer=True)
            self.assertTrue(store.flush(force=True))
            rows = read_history(store.history.path)
            self.assertEqual([r['ft'] for r in rows], [[0, 0], [1, 0], [1, 1], [1, 1]])
            self.assertTrue(rows[-1]['done'])
            self.assertEqual(store.history_health()['pending'], 0)

    def test_deferred_trends_keep_all_changes_without_socket_thread_io(self):
        with tempfile.TemporaryDirectory() as root:
            store = TrendStore(root)
            ticks = [PriceTick('m', '2', 'h', '2.5', 'o', 'Over', 2, value, index)
                     for index, value in enumerate((2, 2.1, 2.2), 1)]
            with patch.object(store, 'flush', wraps=store.flush) as flush:
                for tick in ticks:
                    store.append_many([tick], defer=True)
                flush.assert_not_called()
            self.assertEqual(store.health()['pending'], 3)
            self.assertEqual(store.flush(), 3)
            self.assertEqual([r['new'] for r in store.load('m')], [2, 2.1, 2.2])

    def test_each_score_revision_survives_throttle_failure_and_final_correction(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'scores.json'
            store = ScoreStore(str(path))
            store.save({'m': (0, 0)}, [], force=True)
            with patch.object(store.history, 'append', side_effect=OSError('disk full')):
                self.assertFalse(store.save({'m': (1, 0)}, []))
            self.assertEqual(store.history_health()['pending'], 1)
            # Revision within the latest snapshot's 3-second throttle still archives.
            store.save({'m': (1, 1)}, [])
            store.save({'m': (1, 1)}, ['m'], force=True, half_scores={'m': [1, 0]},
                       final_proofs={'m': {'ms': 3}})
            store.save({'m': (2, 1)}, ['m'], force=True, half_scores={'m': [1, 0]},
                       final_proofs={'m': {'ms': 3}})
            rows = read_history(Path(root) / 'score-history.jsonl.gz')
            self.assertEqual([r['ft'] for r in rows], [[0, 0], [1, 0], [1, 1], [1, 1], [2, 1]])
            self.assertFalse(rows[2]['done'])
            self.assertTrue(rows[3]['done'])
            self.assertEqual(rows[3]['ht'], [1, 0])
            self.assertEqual(ScoreStore(str(path)).load()['m']['ft'], [2, 1])

    def test_all_events_survive_memory_window_and_failed_write(self):
        with tempfile.TemporaryDirectory() as root:
            hub = RealtimeHub(MagicMock(), trend_root=str(Path(root) / '_trends'), resume=False)
            for n in range(125):
                hub._handle_message({'cmd': 'C110', 'cd': {'mid': 'm', 'mc': n, 'unknown': {'n': n}}})
            with patch.object(hub.event_history, 'append', side_effect=OSError('disk full')):
                hub._handle_message({'cmd': 'C103', 'cd': {'mid': 'm', 'msc': 'S1|1:0,S555|3:2,S11001|1:0'}})
            hub.stop()
            rows = read_history(Path(root) / '_live/events.jsonl.gz')
            self.assertEqual(len(rows), 126)
            self.assertLess(len(hub.events('m')), len(rows))
            self.assertEqual(rows[0]['payload']['unknown'], {'n': 0})
            self.assertEqual(rows[-1]['payload']['msc'], 'S1|1:0,S555|3:2,S11001|1:0')
            self.assertEqual(hub.score('m'), (1, 0))
            self.assertEqual(hub.health()['event_history']['pending'], 0)

    def test_score_string_and_list_have_identical_exact_period_semantics(self):
        for raw in ['S10|9:9,S1|2:1,S2|0:1', ['S10|9:9', 'S1|2:1', 'S2|0:1']]:
            self.assertEqual(parse_c103({'mid': 'm', 'msc': raw}), ('m', (2, 1)))
        self.assertIsNone(parse_c103({'mid': 'm', 'msc': 'S1|-1:0'}))


class DecisionHistoryTests(unittest.TestCase):
    def test_every_published_decision_is_frozen_unsampled_and_retryable(self):
        hub = test_live_expert.LiveExpertTests().hub()
        with tempfile.TemporaryDirectory() as root:
            svc = LiveExpertService()
            svc.journal_root = root
            svc.configure(replace(RuntimeConfig(), llm_api_key='fixture-private-key'), 9)
            snapshot = hub.decision_snapshot('m')
            result = svc.compute(snapshot, hub)
            first_probability = result['evaluations'][0]['candidates'][0]['p_model']
            result['evaluations'][0]['candidates'][0]['p_model'] = -1
            snapshot['status']['mst'] = '0'
            # Replay queue no longer drops rows at 512 or samples at 15 seconds.
            with patch.object(svc, '_calculate', return_value={**result, 'evaluations': []}):
                for _ in range(513):
                    svc.compute(hub.decision_snapshot('m'), hub)
            with patch('service.live_expert.HistoryJournal.append', side_effect=OSError('disk full')):
                self.assertEqual(svc.flush(), 0)
            self.assertEqual(svc.health()['journal_pending'], 514)
            self.assertEqual(svc.flush(), 514)
            rows = read_history(Path(root) / 'live-replay.jsonl.gz')
            self.assertEqual(len(rows), 514)
            self.assertEqual(rows[0]['config_version'], 9)
            self.assertEqual(rows[0]['input_state']['status']['mst'], '3600')
            self.assertEqual(rows[0]['evaluations'][0]['candidates'][0]['p_model'], first_probability)
            self.assertTrue(rows[0]['algorithm_performance'])
            self.assertNotIn('llm_api_key', rows[0]['config'])
            self.assertNotIn('fixture-private-key', json.dumps(rows))
            self.assertEqual(svc.health()['journal_dropped'], 0)

    def test_observe_decisions_flush_on_stop_and_old_archives_stay_intact(self):
        hub = test_live_expert.LiveExpertTests().hub()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'live-replay.jsonl'
            path.write_text('legacy archive\n')
            svc = LiveExpertService()
            svc.journal_root = root
            row = svc.compute({**hub.decision_snapshot('m'), 'suspended': True}, hub)
            self.assertEqual(row['decision'], 'observe')
            svc.stop()
            self.assertEqual(path.read_text(), 'legacy archive\n')
            self.assertEqual(read_history(Path(root) / 'live-replay.jsonl.gz')[0]['decision'], 'observe')

    def test_legacy_runs_retry_all_full_decisions_and_preserve_config(self):
        with tempfile.TemporaryDirectory() as root:
            svc = AnalysisService(MagicMock(), config=AnalysisConfig(use_llm=False, result_path=root+'/decisions.json'))
            with patch('service.analysis.HistoryJournal.append', side_effect=OSError('disk full')):
                svc._archive_decision_run({'decisions': [{'match_id': 'm', 'decision': 'observe'}]})
            self.assertEqual(len(svc._decision_history_pending), 1)
            svc._archive_decision_run({'decisions': [{'match_id': 'm', 'decision': 'forecast'}]})
            rows = read_history(Path(root) / 'decision-runs.jsonl.gz')
            self.assertEqual([r['decisions'][0]['decision'] for r in rows], ['observe', 'forecast'])
            self.assertNotIn('llm_api_key', rows[0]['config'])

    def test_llm_review_history_independent_of_experiment_switch(self):
        with tempfile.TemporaryDirectory() as root:
            review = LiveReview()
            review.bind_history(root)
            row = {'match_id': 'm', 'events': [{'cmd': 'C103', 'msc': 'S1|1:0'}]}
            with patch.object(review.history, 'append', side_effect=OSError('disk full')):
                review._record(row, {'status': 'ready', 'verdict': 'watch'})
            row['events'].clear()
            review._record(row, {'status': 'ready', 'verdict': 'reject'})
            rows = read_history(Path(root) / 'llm-reviews.jsonl.gz')
            self.assertEqual([r['review']['verdict'] for r in rows], ['watch', 'reject'])
            self.assertTrue(rows[0]['decision']['events'])


class EventModellingTests(unittest.TestCase):
    def test_raw_cards_corners_do_not_directly_change_mathematical_model(self):
        hub = test_live_expert.LiveExpertTests().hub()
        svc = LiveExpertService()
        before = svc.compute(hub.decision_snapshot('m'), hub)
        hub._handle_message({'cmd': 'C110', 'cd': {'mid': 'm', 'mc': 999}})
        snapshot = hub.decision_snapshot('m')
        snapshot['status']['msc'] = 'S1|1:0,S555|6:1,S12001|3:0,S11001|1:0'
        after = svc.compute(snapshot, hub)
        self.assertEqual(before['evaluations'], after['evaluations'])
        self.assertEqual(before['picks'], after['picks'])
        self.assertNotEqual(before['events'], after['events'])

    def test_score_clock_quotes_and_pause_can_change_forecast_or_decision(self):
        hub = test_live_expert.LiveExpertTests().hub()
        svc = LiveExpertService()
        before = svc.compute(hub.decision_snapshot('m'), hub)
        snapshot = hub.decision_snapshot('m')
        later = svc.compute({**snapshot, 'status': {**snapshot['status'], 'mst': '5000'}}, hub)
        self.assertNotEqual(before['remaining_goals'], later['remaining_goals'])
        goal = svc.compute({**snapshot, 'score': (2, 0)}, hub)
        self.assertNotEqual(before['evaluations'], goal['evaluations'])
        test_live_expert.LiveExpertTests().ticks(hub, over=2.3)
        repriced = svc.compute(hub.decision_snapshot('m'), hub)
        self.assertNotEqual(goal['evaluations'], repriced['evaluations'])
        paused = svc.compute({**hub.decision_snapshot('m'), 'suspended': True}, hub)
        self.assertNotEqual(before['decision'], paused['decision'])
        self.assertFalse(paused['picks'])


class LedgerAndShutdownTests(unittest.TestCase):
    def test_ledger_append_never_rotates_and_preserves_settlement_revisions(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = DecisionLedger(root)
            row = LedgerEntry(at='2026-01-01', match_id='m', decision_id='m', is_pick=True,
                              market='OU', line='2.5', outcome='over', odds=2)
            ledger._append([row])
            path = Path(root) / 'ledger.jsonl'
            before = path.read_bytes()
            # Old rotation threshold evaluated stat().st_size; mock only that check.
            real_stat = Path.stat
            def over_old_limit(target, *args, **kwargs):
                stat = real_stat(target, *args, **kwargs)
                if target == path:
                    return SimpleNamespace(st_size=65*1024**2, st_mtime_ns=stat.st_mtime_ns, st_mode=stat.st_mode)
                return stat
            with patch.object(Path, 'stat', autospec=True, side_effect=over_old_limit):
                ledger._append([replace(row, updated_at='2026-01-02')])
            self.assertTrue(path.read_bytes().startswith(before))
            ledger.settle({'m': {'ft': [2, 1], 'done': True}})
            ledger.settle({'m': {'ft': [1, 0], 'done': True}}, regrade=True)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([r['status'] for r in rows[-2:]], ['won', 'lost'])
            self.assertEqual(ledger.load()[0].status, 'lost')
            self.assertEqual(list(Path(root).glob('ledger.*.jsonl')), [])

    def test_sigterm_runs_cleanup_before_server_close(self):
        from api import app as api_app
        server = MagicMock()
        app = MagicMock()
        hub = MagicMock()
        previous = signal.getsignal(signal.SIGTERM)
        def send_termination():
            handler = signal.getsignal(signal.SIGTERM)
            if callable(handler):
                handler(signal.SIGTERM, None)
            else:
                raise AssertionError('SIGTERM handler not installed')
        server.serve_forever.side_effect = send_termination
        with patch.object(api_app, 'create_app', return_value=app), \
                patch.object(api_app, '_start_background', return_value=hub), \
                patch.object(api_app, 'make_server', return_value=server), \
                patch.dict('os.environ', {'REALTIME_DISABLED': '0'}):
            api_app.run_server()
        hub.stop.assert_called_once()
        app._analysis.stop_scheduler.assert_called_once()
        server.server_close.assert_called_once()
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
