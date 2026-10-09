import unittest
from dataclasses import replace

from service.betting import preview_bet, plan_bet
from service.runtime_settings import RuntimeConfig, validated


class BettingTests(unittest.TestCase):
    def pick(self, **changes):
        value = {
            'match_id': 'm1', 'market': 'OU', 'line': '2.5', 'outcome': 'over',
            'confidence': .8, 'hit_count': 4,
        }
        value.update(changes)
        return value

    def test_default_is_disabled_and_never_creates_plan(self):
        result = preview_bet(self.pick(), RuntimeConfig())
        self.assertFalse(result['allowed'])
        self.assertIn('未启用', result['reason'])

    def test_hit_gate_and_stake_modes(self):
        cfg = replace(RuntimeConfig(), betting_enabled=True, betting_fixed_stake=20,
                      betting_stake_mode='confidence_multiplier', betting_min_hit_count=3)
        plan = plan_bet(self.pick(), cfg)
        self.assertEqual(plan.stake, 16.0)
        self.assertFalse(plan.executable)
        blocked = preview_bet(self.pick(hit_count=2), cfg)
        self.assertFalse(blocked['allowed'])
        self.assertIn('命中次数不足', blocked['reason'])

    def test_multiplier_prefers_ensemble_confidence(self):
        cfg = replace(RuntimeConfig(), betting_enabled=True, betting_fixed_stake=20,
                      betting_stake_mode='confidence_multiplier', betting_min_hit_count=3)
        plan = plan_bet(self.pick(confidence=.2, composite_confidence=.75), cfg)
        self.assertEqual(plan.stake, 15.0)

    def test_live_and_market_gates(self):
        cfg = replace(RuntimeConfig(), betting_enabled=True)
        self.assertFalse(preview_bet(self.pick(), cfg, match_live=False)['allowed'])
        self.assertFalse(preview_bet(self.pick(), cfg, market_open=False)['allowed'])

    def test_invalid_stake_mode_rejected_by_settings(self):
        with self.assertRaises(ValueError):
            validated(RuntimeConfig(), {'betting_stake_mode': 'kelly'})


if __name__ == '__main__':
    unittest.main()
