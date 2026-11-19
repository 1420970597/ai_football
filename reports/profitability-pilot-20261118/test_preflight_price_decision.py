from copy import deepcopy
from decimal import Decimal
import unittest

from preflight_price_decision import compare_with_receipts, evaluate


class PreflightPriceDecisionTests(unittest.TestCase):
    def setUp(self):
        contract = {
            'fixture_id': 'leyu:123', 'sport': 'football', 'market_id': '2', 'kind': 'total',
            'phase': 'scheduled', 'period': 'full_time', 'line': '2.5', 'score_basis': 'full_period',
            'settlement_scope': 'regulation',
        }
        book = {'bid': '0.59', 'ask': '0.61', 'bid_size': '13', 'ask_size': '16',
                'quoted_at': '2026-11-19T11:59:20Z', 'received_at': '2026-11-19T11:59:22Z'}
        over = dict(book, canonical_outcome='over')
        under = dict(book, bid='0.39', ask='0.41', canonical_outcome='under')
        references = []
        sources = {}
        for name in ('a', 'b'):
            digest = name * 64
            references.append({'source': name, 'market': f'{name}-total', 'rules_sha256': digest,
                               'status': 'timed_depth', 'outcome_status': 'mapped',
                               'books': [dict(over), dict(under)]})
            sources[name] = {'venue': name, 'feed_origin': f'feed-{name}', 'minimum_book_depth': '12',
                             'rules': {f'{name}-total': digest}}
        self.row = {
            'preflight': contract | {'selection': 'over', 'effective_score_basis': 'full_period', 'price': '1.80',
                                     'captured_at': '2026-11-19T11:59:25Z',
                                     'provider_quote_checked_at': '2026-11-19T11:59:23Z',
                                     'source_updated_at': '2026-11-19T11:58:45Z'},
            'references': references,
            'source_groups': {name: {'status': 'complete'} for name in sources},
        }
        self.approval = {'contract': contract, 'approved_at': '2026-11-19T11:30:00Z', 'cost_per_unit': '0.01',
                         'cost_evidence': 'verified cost schedule', 'settlement_evidence': 'reviewed terms',
                         'required_net_edge': '0.02', 'offer_feed_origin': 'leyu-feed', 'sources': sources}

    def evaluate(self):
        return evaluate(self.row, [self.approval], 30)

    def test_price_rule_accepts_above_floor_and_rejects_below(self):
        result = self.evaluate()
        self.assertEqual('price_eligible', result['status'])
        self.assertEqual(Decimal('1.03') / Decimal('0.59'), result['pricing']['minimum_odds'])
        self.row['preflight']['price'] = '1.73'
        self.assertEqual('below_minimum_odds', self.evaluate()['status'])

    def test_cost_terms_and_identity_must_be_available_at_the_decision(self):
        self.approval['approved_at'] = '2026-11-19T11:59:25.000001Z'
        self.assertIn('settlement_and_cost_approval_missing', self.evaluate()['reasons'])
        self.approval['approved_at'] = '2026-11-19T11:30:00Z'
        self.approval['contract'] = dict(self.approval['contract'], market_id='18')
        self.assertIn('settlement_and_cost_approval_missing', self.evaluate()['reasons'])

    def test_checks_source_depth_hash_origin_and_native_time(self):
        for failure, reason in (
            ('depth', 'two_approved_depth_sources_missing'),
            ('rules', 'two_approved_depth_sources_missing'),
            ('origin', 'reference_validation_failed'),
            ('check', 'native_offer_check_time_unverified'),
            ('delay', 'reference_validation_failed'),
        ):
            with self.subTest(failure=failure):
                row, approval = deepcopy(self.row), deepcopy(self.approval)
                if failure == 'depth':
                    row['references'][1]['books'][0]['ask_size'] = '11.999'
                elif failure == 'rules':
                    row['references'][1]['rules_sha256'] = 'f' * 64
                elif failure == 'origin':
                    approval['sources']['b']['feed_origin'] = approval['sources']['a']['feed_origin']
                elif failure == 'check':
                    row['preflight']['provider_quote_checked_at'] = '2026-11-19T11:59:25.1Z'
                elif failure == 'delay':
                    row['references'][1]['books'][0]['quoted_at'] = '2026-11-19T11:59:17Z'
                self.assertIn(reason, evaluate(row, [approval], 30)['reasons'])

    def test_live_score_agreement_alone_does_not_establish_the_game_state(self):
        self.row['preflight']['phase'] = self.approval['contract']['phase'] = 'live'
        self.row['score_comparison'] = {'status': 'observed_score_matches'}
        self.assertEqual(['live_state_unverified'], self.evaluate()['reasons'])

    def test_missing_source_quotes_and_cost_proof_are_visible(self):
        self.row.update(references=[], source_groups={})
        self.assertEqual(['reference_contract_missing', 'settlement_and_cost_approval_missing'],
                         evaluate(self.row, [], 30)['reasons'])

    def test_policy_comparison_excludes_manual_and_unverified_amounts(self):
        native = {'manual': False, 'settlement_status': 'locally_matched_receipt',
                  'receipt_currency': 'CNY', 'recorded_receipt_net': '1.20'}
        samples = [
            (native, 'price_eligible'),
            (dict(native, recorded_receipt_net='-5'), 'below_minimum_odds'),
            (dict(native, settlement_status='settled_missing_cost_or_currency'), 'ineligible'),
            (dict(native, manual=True), 'price_eligible'),
        ]
        rows = [{'preflight': order, 'price_decision': {'status': status}} for order, status in samples]
        report = compare_with_receipts(rows)
        self.assertEqual(3, report['current_following']['selected'])
        self.assertEqual({'CNY': Decimal('-3.80')}, report['current_following']['net_by_currency'])
        self.assertEqual(1, report['current_following']['receipt_statuses']['settled_missing_cost_or_currency'])
        self.assertEqual({'CNY': Decimal('1.20')}, report['price_rule']['net_by_currency'])
        self.assertEqual(0, report['abstain']['selected'])
