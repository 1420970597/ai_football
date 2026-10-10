import unittest
from dataclasses import replace

from service.betting import (build_ybty_bet_payload, draft_ybty_bet,
                              preview_bet, plan_bet)
from service.runtime_settings import RuntimeConfig, validated


class BettingTests(unittest.TestCase):
    def pick(self, **changes):
        value = {
            'match_id': 'm1', 'market': 'OU', 'line': '2.5', 'outcome': 'over',
            'confidence': .8, 'hit_count': 4, 'algorithm': 'economic_ensemble',
        }
        value.update(changes)
        return value

    def test_default_is_disabled_and_never_creates_plan(self):
        result = preview_bet(self.pick(), RuntimeConfig())
        self.assertFalse(result['allowed'])
        self.assertIn('未启用', result['reason'])
        self.assertFalse(result['submitted'])
        self.assertTrue(result['execution']['execution_supported'])

    def test_enabled_gate_still_reports_preview_without_execution(self):
        cfg = replace(RuntimeConfig(), betting_enabled=True)
        result = preview_bet(self.pick(), cfg)
        self.assertTrue(result['allowed'])
        self.assertTrue(result['execution']['configured_enabled'])
        self.assertTrue(result['execution']['execution_enabled'])
        self.assertFalse(result['submitted'])
        self.assertTrue(result['plan']['executable'])
        self.assertEqual(result['submission'], 'preview_only')
        self.assertIn('自动提交', result['execution']['reason'])

    def test_hit_gate_and_stake_modes(self):
        cfg = replace(RuntimeConfig(), betting_enabled=True, betting_fixed_stake=20,
                      betting_stake_mode='confidence_multiplier', betting_min_hit_count=3)
        plan = plan_bet(self.pick(), cfg)
        self.assertEqual(plan.stake, 16.0)
        self.assertTrue(plan.executable)
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

    def test_individual_algorithm_is_never_bet(self):
        cfg = replace(RuntimeConfig(), betting_enabled=True, betting_fixed_stake=2)
        result = preview_bet(self.pick(algorithm='poisson_market'), cfg)
        self.assertFalse(result['allowed'])
        self.assertIn('综合推荐', result['reason'])

    def test_ensemble_fixed_two_yuan_draft(self):
        cfg = replace(RuntimeConfig(), betting_enabled=True, betting_fixed_stake=2,
                      betting_stake_mode='fixed', betting_min_hit_count=1)
        result = draft_ybty_bet(self.pick(
            matchId='5676526', marketId='144205358038450133', playId=1,
            playOptions='1', playOptionsId='141234580127215829',
            oddFinally='1.69'), cfg)
        self.assertTrue(result['allowed'])
        self.assertEqual(result['plan']['stake'], 2.0)
        self.assertEqual(result['payload']['seriesOrders'][0]['orderDetailList'][0]['betAmount'], '2.00')
        self.assertEqual(result['submission'], 'manual_only')

    def test_invalid_stake_mode_rejected_by_settings(self):
        with self.assertRaises(ValueError):
            validated(RuntimeConfig(), {'betting_stake_mode': 'kelly'})

    def test_builds_app_shape_without_network_submission(self):
        cfg = replace(RuntimeConfig(), betting_enabled=True, betting_fixed_stake=20,
                      betting_stake_mode='confidence_multiplier', betting_min_hit_count=3)
        pick = self.pick(
            confidence=.2, composite_confidence=.75,
            matchId='5676526', marketId='144205358038450133', playId=1,
            playOptions='1', playOptionsId='141234580127215829',
            oddFinally='1.69', marketValue='切尔西', marketType='EU',
            details=[{'matchId': '5676526', 'marketId': '144205358038450133',
                      'playId': 1, 'playOptions': '1',
                      'playOptionsId': '141234580127215829', 'oddFinally': '1.69'}])
        payload = build_ybty_bet_payload(pick, 15)
        detail = payload['seriesOrders'][0]['orderDetailList'][0]
        self.assertEqual(payload['preBet'], '0')
        self.assertEqual(payload['acceptOdds'], 2)
        self.assertIs(type(payload['acceptOdds']), int)
        self.assertEqual(payload['deviceType'], '3')
        self.assertEqual(detail['matchId'], '5676526')
        self.assertEqual(detail['betAmount'], '15.00')
        self.assertEqual(detail['playId'], '1')
        self.assertEqual(detail['sportId'], '1')
        self.assertEqual(detail['odds'], '169000')

        draft = draft_ybty_bet(pick, cfg)
        self.assertTrue(draft['allowed'])
        self.assertTrue(draft['requires_manual_confirmation'])
        self.assertEqual(draft['submission'], 'manual_only')
        self.assertFalse(draft['submitted'])
        self.assertTrue(draft['execution']['execution_enabled'])

    def test_app_payload_requires_provider_identifiers(self):
        with self.assertRaisesRegex(ValueError, '缺少 App 字段'):
            build_ybty_bet_payload(self.pick(), 10)

    def test_chinese_order_names_preserve_protocol_selection(self):
        for market, outcome, native, line, name, option in (
            ('AH', 'away', '2', '-0.5', '全场让球', '客队全场+0.5'),
            ('AH_1H', 'home', '1', '0/0.5', '上半场让球', '主队上半场+0/0.5'),
            ('OU', 'over', 'Over', '2.5', '全场大小', '全场进球数>2.5'),
            ('OU_1H', 'under', 'Under', '1.5', '上半场大小', '上半场进球数<1.5'),
            ('HAD', 'draw', 'X', '', '全场独赢', '全场平局'),
        ):
            with self.subTest(market=market, outcome=outcome):
                pick = self.pick(market=market, outcome=outcome, line=line, home='主队', away='客队', league='中文联赛',
                    order_detail={'matchId': 'm1', 'marketId': 'market1', 'playId': '4',
                                  'playOptions': native, 'playOptionsId': 'option1', 'oddFinally': '1.95',
                                  'marketValue': line, 'playName': 'English market', 'playOptionName': 'English option'})
                detail = build_ybty_bet_payload(pick, 2)['seriesOrders'][0]['orderDetailList'][0]
                self.assertEqual(detail['matchInfo'], '主队 v 客队')
                self.assertEqual(detail['matchName'], '中文联赛')
                self.assertEqual(detail['sportName'], '足球')
                self.assertEqual(detail['playName'], name)
                self.assertEqual(detail['playOptionName'], option)
                self.assertEqual(detail['playOptions'], native)
                self.assertEqual(detail['playOptionsId'], 'option1')
                self.assertEqual(detail['marketValue'], line)
                self.assertEqual(detail['odds'], '195000')

    def test_native_chinese_display_names_are_preserved(self):
        pick = self.pick(home='主队', away='客队', league='联赛', order_detail={
            'matchId': 'm1', 'marketId': 'market1', 'playId': '2', 'playOptions': 'Over',
            'playOptionsId': 'option1', 'oddFinally': '1.95', 'playName': '全场大小球',
            'playOptionName': '大 2.5', 'matchInfo': '主队VS客队', 'matchName': '官方中文联赛',
            'sportName': '足球'})
        detail = build_ybty_bet_payload(pick, 2)['seriesOrders'][0]['orderDetailList'][0]
        for key in ('playName', 'playOptionName', 'matchInfo', 'matchName', 'sportName'):
            self.assertEqual(detail[key], pick['order_detail'][key])

    def test_draft_without_team_metadata_does_not_invent_team_names(self):
        pick = self.pick(order_detail={'matchId': 'm1', 'marketId': 'market1', 'playId': '2',
                                      'playOptions': 'Over', 'playOptionsId': 'option1', 'oddFinally': '1.95'})
        detail = build_ybty_bet_payload(pick, 2)['seriesOrders'][0]['orderDetailList'][0]
        self.assertNotIn('matchInfo', detail)
        self.assertNotIn('matchName', detail)
        self.assertEqual(detail['playOptions'], 'Over')

    def test_incomplete_draft_reports_both_gate_and_execution_status(self):
        cfg = replace(RuntimeConfig(), betting_enabled=True)
        result = draft_ybty_bet(self.pick(), cfg)
        self.assertFalse(result['allowed'])
        self.assertFalse(result['submitted'])
        self.assertEqual(result['submission'], 'blocked')
        self.assertIn('缺少 App 字段', result['reason'])
        self.assertEqual(result['execution']['mode'], 'automatic_single')


if __name__ == '__main__':
    unittest.main()
