"""Regression tests for TASK-58: pre-goal prices must never become picks or orders."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from collector.leyu_realtime import PriceTick, RealtimeHub
from core.live_model import competition_type
from service.betting import BettingExecutor, BettingRecomputeNeeded
from service.ledger import DecisionLedger
from service.live_expert import LiveExpertService
from service.runtime_settings import RuntimeConfig, validated


def make_hub(tick_ms=None):
    hub = RealtimeHub(MagicMock(), resume=False)
    now = time.monotonic()
    hub._info['m'] = {'match_id': 'm', 'league': '真实足球联赛', 'home': '甲', 'away': '乙', 'sport_id': '1'}
    hub._scores['m'] = (0, 0)
    hub._score_at['m'] = now
    hub._status_at['m'] = now
    hub._status['m'] = {'mmp': '6', 'mst': '1800'}
    ts = tick_ms or int(time.time() * 1000)
    ticks = [PriceTick('m', chpid, chpid, line, oc, oc, odds, odds, ts)
             for chpid, line, quotes in [('2', '2.25', [('Over', 1.9), ('Under', 1.98)]),
                                         ('1', '', [('1', 2.4), ('X', 3.2), ('2', 3.1)])]
             for oc, odds in quotes]
    hub._record_ticks(ticks)
    return hub


class HubScoreChangeTests(unittest.TestCase):
    def test_first_score_is_not_a_change_but_a_new_score_is(self):
        hub = RealtimeHub(MagicMock(), resume=False)
        with hub._lock:
            hub._set_score('m', (0, 0))
        self.assertIsNone(hub.score_changed_at_ms('m'))
        before = int(time.time() * 1000)
        with hub._lock:
            hub._set_score('m', (0, 0))
        self.assertIsNone(hub.score_changed_at_ms('m'))
        with hub._lock:
            hub._set_score('m', (0, 1))
        self.assertGreaterEqual(hub.score_changed_at_ms('m'), before)
        self.assertEqual(hub.decision_snapshot('m')['score_changed_at_ms'], hub.score_changed_at_ms('m'))


class LiveCooldownTests(unittest.TestCase):
    def test_recent_score_change_makes_every_market_stale(self):
        hub = make_hub()
        svc = LiveExpertService()
        snapshot = hub.decision_snapshot('m')
        snapshot['score_changed_at_ms'] = snapshot['captured_at_ms'] - 5000
        row = svc.compute(snapshot, hub)
        self.assertTrue(row['score_cooling'])
        self.assertFalse(row['picks'])
        self.assertFalse(any(m['fresh'] for m in row['markets']))
        self.assertTrue(any('比分刚变化' in r for r in row['reasons']))

    def test_quotes_older_than_change_plus_cooldown_stay_stale_after_cooldown(self):
        # Change happened 60s ago, but the quotes were stamped right after it:
        # those may be the venue's pre-repricing prices and must not be used.
        now_ms = int(time.time() * 1000)
        hub = make_hub(tick_ms=now_ms - 1000)
        svc = LiveExpertService()
        snapshot = hub.decision_snapshot('m')
        # Change 50s ago; quotes from 1s ago are after change + 45s cooldown.
        snapshot['score_changed_at_ms'] = snapshot['captured_at_ms'] - 50_000
        row = svc.compute(snapshot, hub)
        self.assertFalse(row['score_cooling'])
        self.assertTrue(any(m['fresh'] for m in row['markets']))
        # Change 50s ago but quotes stamped 10s after it, still inside the cooldown.
        hub2 = make_hub(tick_ms=now_ms - 40_000)
        snap2 = hub2.decision_snapshot('m')
        snap2['score_changed_at_ms'] = snap2['captured_at_ms'] - 50_000
        svc2 = LiveExpertService()
        svc2.configure(RuntimeConfig(quote_max_age_s=300.0), 2)
        row2 = svc2.compute(snap2, hub2)
        self.assertFalse(row2['score_cooling'])
        self.assertFalse(any(m['fresh'] for m in row2['markets']))
        self.assertFalse(row2['picks'])

    def test_cooldown_zero_keeps_previous_behaviour(self):
        hub = make_hub()
        svc = LiveExpertService()
        svc.configure(RuntimeConfig(score_change_cooldown_s=0.0), 2)
        snapshot = hub.decision_snapshot('m')
        snapshot['score_changed_at_ms'] = snapshot['captured_at_ms'] - 1000
        row = svc.compute(snapshot, hub)
        self.assertFalse(row['score_cooling'])
        self.assertTrue(any(m['fresh'] for m in row['markets']))

    def test_setting_is_validated(self):
        base = RuntimeConfig()
        self.assertEqual(validated(base, {'score_change_cooldown_s': 60}).score_change_cooldown_s, 60)
        with self.assertRaises(ValueError):
            validated(base, {'score_change_cooldown_s': -1})


class ExecutorCooldownTests(unittest.TestCase):
    def check(self, *, changed_ago_s, quote_after_change_s):
        now_ms = time.time() * 1000
        changed = now_ms - changed_ago_s * 1000
        quote = SimpleNamespace(oid='o', hv='0.5', ot='1', chpid='4', odds=1.9,
                                ts_ms=changed + quote_after_change_s * 1000, quote_age_s=1.0,
                                order_detail={})
        detail = {'playOptionsId': 'o', 'playOptions': '1', 'playId': '4', 'chpid': '4',
                  'oddFinally': '1.9', 'matchId': 'm', 'marketId': 'k'}
        snapshot = {'score': (0, 1), 'status': {'mmp': '6'}, 'score_age_s': 1, 'status_age_s': 1,
                    'quotes': [quote], 'score_changed_at_ms': changed}
        row = {'published_at_ms': now_ms, 'score': (0, 1), 'phase': '6'}
        cfg = RuntimeConfig(betting_enabled=True)
        executor = BettingExecutor.__new__(BettingExecutor)
        return executor._check_state(row, {'line': '0.5'}, snapshot, detail, cfg)

    def test_rejects_during_cooldown(self):
        with self.assertRaisesRegex(BettingRecomputeNeeded, '比分刚变化'):
            self.check(changed_ago_s=5, quote_after_change_s=1)

    def test_rejects_quote_stamped_inside_cooldown(self):
        with self.assertRaisesRegex(BettingRecomputeNeeded, '早于比分变化'):
            self.check(changed_ago_s=120, quote_after_change_s=10)

    def test_accepts_repriced_quote(self):
        self.assertIsNone(self.check(changed_ago_s=120, quote_after_change_s=60))


class VirtualAndLedgerTests(unittest.TestCase):
    def test_duration_labelled_tournaments_are_virtual(self):
        for league in ('瓦尔基里杯 2026 (8分钟)', '瓦尔哈拉杯_2026_8分钟', 'Esoccer Battle - 8 mins play'):
            self.assertEqual(competition_type({'league': league, 'sport_id': '1'}), 'virtual', league)
        for league, home in (('意大利甲级联赛', '都灵'), ('巴西甲级联赛', 'Volta Redonda'),
                             ('玻利维亚西蒙玻利瓦尔杯', '甲')):
            self.assertEqual(competition_type({'league': league, 'home': home, 'sport_id': '1'}), 'real')

    def test_live_ledger_entry_keeps_quote_and_score_change_timestamps(self):
        forecast = {'market': 'AH', 'line': '0.5', 'outcome': 'home', 'odds': 1.9, 'ev': .05,
                    'p_market': .5, 'p_model': .55, 'settlement_basis': 'remaining_score',
                    'ts_ms': 1791830820000, 'quote_received_at_ms': 1791830820500}
        row = {'match_id': 'm', 'algorithm': 'economic_ensemble', 'competition_type': 'real',
               'computed_at': '2026-10-13T00:00:00+00:00', 'score': [0, 1], 'elapsed_s': 1800,
               'model_version': 'v', 'config_version': 1, 'forecast': forecast,
               'score_changed_at_ms': 1791830800000}
        entry = DecisionLedger._live_entry(row)
        self.assertEqual(entry.quote_ts_ms, 1791830820000)
        self.assertEqual(entry.quote_received_at_ms, 1791830820500)
        self.assertEqual(entry.score_changed_at_ms, 1791830800000)


if __name__ == '__main__':
    unittest.main()
