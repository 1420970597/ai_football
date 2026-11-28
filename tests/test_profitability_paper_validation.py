from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from infra.profitability.paper_settle import frames_digest
from infra.profitability import paper_validate
from infra.profitability.paper_validate import PRIMARY_ARM, registration, run


START = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
ONE_MINUTE = timedelta(minutes=1)
SAMPLE_BINDING = 'a' * 64


def input_documents():
    observed = START + timedelta(seconds=5)
    checked = START + timedelta(seconds=6)
    offer = {'status': 'native_preflight_available', 'signal_id': 'signal', 'fixture_id': 'leyu:12',
             'expert_id': 'expert', 'market_kind': 'total', 'market_id': '2', 'selection': 'over',
             'period': 'full_time', 'sport': 'football', 'phase': 'scheduled', 'line': '2.5',
             'score_basis': 'full_period', 'score_baseline': {}, 'paper_account_binding': SAMPLE_BINDING,
             'paper_amount_passing_native_limits': '5', 'native_prices': {'Over': '1.80', 'Under': '2.10'},
             'native_selected_price': '1.80', 'native_quote_checked_at': checked.isoformat(),
             'created_at': (START - ONE_MINUTE).isoformat()}
    frames = [{'slot': START.isoformat(), 'observed_at': observed.isoformat(), 'rows': [offer],
               'begin': (START - timedelta(seconds=90)).isoformat(),
               'end': (START - timedelta(seconds=30)).isoformat()}]
    settlement = {key: offer[key] for key in ('signal_id', 'fixture_id', 'sport', 'phase', 'market_kind',
                                               'native_quote_checked_at', 'native_selected_price')}
    settlement.update(status='paper_quote_diagnostic', outcome='win', result_version=1, quoted_unit_return='0.80',
                      stressed_unit_return_before_unknown_costs='0.784',
                      result_received_at=(START + timedelta(hours=2)).isoformat(),
                      result_computed_at=(START + timedelta(hours=2, seconds=1)).isoformat())
    return frames, {'cutoff': (START + timedelta(days=1)).isoformat(), 'frames_sha256': frames_digest(frames),
                    'paper_settlements': [settlement]}


class PaperValidationTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'books').mkdir()
        self.evidence = b'fixture for a documented native cost schedule'
        self.digest = sha256(self.evidence).hexdigest()
        (self.root / self.digest).write_bytes(self.evidence)
        self.contract = {'fixture_id': 'leyu:12', 'sport': 'football', 'market_kind': 'total', 'kind': 'total',
                         'market_id': '2', 'phase': 'scheduled', 'period': 'full_time', 'line': '2.5',
                         'score_basis': 'full_period', 'settlement_scope': 'regulation'}
        self.rules = {'polymarket': 'a' * 64, 'kalshi': 'b' * 64}
        self.sources = {name: {'venue': name, 'feed_origin': f'{name}-direct', 'minimum_book_depth': '10',
                               'rules': {name: digest}} for name, digest in self.rules.items()}
        self.approval = {'contract': self.contract, 'approved_at': (START - ONE_MINUTE).isoformat(),
                         'required_net_edge': '0.02', 'cost_per_unit': '0.01', 'cost_evidence': 'document',
                         'settlement_evidence': 'native terms', 'offer_feed_origin': 'leyu-direct',
                         'cost_schedule': {'evidence_sha256': self.digest, 'account_binding': SAMPLE_BINDING,
                                           'currency': 'CNY', 'stake_min': '1', 'stake_max': '10',
                                           'effective_from': (START - ONE_MINUTE).isoformat()},
                         'sources': self.sources}
        self.fixture = {'label': 'fixture', 'bindings': [
            self.contract | {'source': name, 'market': name, 'rules_sha256': digest} for name, digest in self.rules.items()
        ], 'outcomes': {
            'polymarket': {'polymarket': {'rules_sha256': self.rules['polymarket'],
                                         'labels': {'Over': 'over', 'Under': 'under'}}},
            'kalshi': {'kalshi': {'rules_sha256': self.rules['kalshi'], 'labels': {'yes': 'over'}}},
        }, 'approvals': [self.approval]}

    def prepare_reference(self, *, later_venue=None):
        (self.root / 'reference-price.json').write_text(json.dumps({'fixtures': {'leyu:12': self.fixture}}))
        markets = []
        for name, digest in self.rules.items():
            quote = START + timedelta(seconds=7 if name == later_venue else 3)
            book = {'status': 'two_sided', 'quote_time': quote.isoformat(), 'received_at': quote.isoformat(),
                    'bid': '0.59', 'ask': '0.61', 'bid_size': '12', 'ask_size': '12'}
            books = ([book | {'outcome': 'Over'}, book | {'outcome': 'Under', 'bid': '0.39', 'ask': '0.41'}]
                     if name == 'polymarket' else [book | {'outcome': 'yes'}])
            markets.append({'source': name, 'market': name, 'rules_sha256': digest,
                            'accepting_orders': True, 'status': 'active', 'books': books})
        (self.root / 'books/fixture-observed.jsonl').write_text(
            json.dumps({'captured_at': (START + timedelta(seconds=8)).isoformat(), 'markets': markets}) + '\n',
        )

    def report(self, *, registered_at=START):
        frames, results = input_documents()
        return run(frames, results, START, START + ONE_MINUTE, self.root, self.root,
                   registered_at=registered_at)

    def test_replays_two_timed_books_through_cost_and_settlement(self):
        self.prepare_reference()
        report = self.report()
        arm = report['arms'][PRIMARY_ARM]
        self.assertEqual(1, arm['selected'])
        self.assertEqual(1, arm['settled'])
        self.assertEqual(Decimal('0.774'), arm['stressed_units'])
        self.assertEqual(0, report['missing_slots'])
        self.assertEqual({}, report['exclusions'])
        self.assertRegex(report['reference_matches_sha256'], r'^[a-f0-9]{64}$')
        self.assertIn('four_positive_complete_weeks_missing', arm['validation_blockers'])
        self.assertIn('fewer_than_200_fixtures', arm['validation_blockers'])

    def test_market_observed_after_placement_cannot_retroactively_qualify(self):
        self.prepare_reference(later_venue='kalshi')
        report = self.report()
        self.assertEqual(0, report['arms'][PRIMARY_ARM]['selected'])
        self.assertEqual(1, report['exclusions']['two_complete_sources_missing'])

    def test_cost_must_match_hashed_document_binding_limits_and_effective_time(self):
        base = self.approval['cost_schedule'].copy()
        for changes in ({'account_binding': 'e' * 64}, {'stake_max': '4'}, {'currency': 'cny'},
                        {'effective_from': (START + ONE_MINUTE).isoformat()}, {'evidence_sha256': 'd' * 64}):
            with self.subTest(changes=changes):
                self.approval['cost_schedule'] = base | changes
                self.prepare_reference()
                self.assertEqual({'account_cost_scope_unverified': 1}, self.report()['exclusions'])
        self.approval['cost_schedule'] = base
        self.prepare_reference()
        (self.root / self.digest).write_bytes(b'changed')
        self.assertEqual({'account_cost_scope_unverified': 1}, self.report()['exclusions'])

    def test_eligibility_uses_stressed_offer_and_retains_pending_in_selection(self):
        self.approval['required_net_edge'] = '0.05'
        self.prepare_reference()
        report = self.report()
        self.assertEqual(1, report['arms']['follow_priced']['settled'])
        self.assertEqual(0, report['arms'][PRIMARY_ARM]['selected'])
        self.approval['required_net_edge'] = '0.02'
        self.prepare_reference()
        frames, settlement = input_documents()
        settlement['paper_settlements'] = []
        report = run(frames, settlement, START, START + ONE_MINUTE, self.root, self.root)
        arm = report['arms'][PRIMARY_ARM]
        self.assertEqual(1, arm['selected'])
        self.assertIn('unsettled_or_unverified_receipts', arm['validation_blockers'])
        self.assertIn('retrospective_research', arm['validation_blockers'])

    def test_reference_approval_cannot_reduce_the_registered_minimum_edge(self):
        self.approval['required_net_edge'] = '0.0001'
        self.prepare_reference()
        frames, settlement = input_documents()
        offer = frames[0]['rows'][0]
        offer.update(native_selected_price='1.75', native_prices={'Over': '1.75', 'Under': '2.10'})
        settlement['frames_sha256'] = frames_digest(frames)
        settlement['paper_settlements'][0].update(
            native_selected_price='1.75', quoted_unit_return='0.75', stressed_unit_return_before_unknown_costs='0.735',
        )
        report = run(frames, settlement, START, START + ONE_MINUTE, self.root, self.root)
        self.assertEqual(1, report['arms']['follow_priced']['settled'])
        self.assertEqual(0, report['arms'][PRIMARY_ARM]['selected'])

    def test_frozen_settlements_cannot_score_a_mutated_sample(self):
        self.prepare_reference()
        frames, results = input_documents()
        frames[0]['rows'][0]['native_selected_price'] = '1.90'
        with self.assertRaisesRegex(ValueError, 'matching frozen input'):
            run(frames, results, START, START + ONE_MINUTE, self.root, self.root)

    def test_settlement_profit_must_reproduce_the_recorded_price_and_outcome(self):
        self.prepare_reference()
        frames, results = input_documents()
        results['paper_settlements'][0]['stressed_unit_return_before_unknown_costs'] = '0.80'
        with self.assertRaisesRegex(ValueError, 'invalid paper settlement receipt'):
            run(frames, results, START, START + ONE_MINUTE, self.root, self.root)

    def test_registration_binds_the_implementation_and_fixed_window(self):
        end = START + timedelta(days=28)
        record = {'strategy': PRIMARY_ARM, 'start': START.isoformat(), 'end': end.isoformat(),
                  'registered_at': (START - ONE_MINUTE).isoformat(), 'implementation_commit': 'a' * 40,
                  'evaluator_sha256': sha256(Path(paper_validate.__file__).read_bytes()).hexdigest()}
        source = self.root / 'registration.json'
        source.write_text(json.dumps(record))
        self.assertEqual('a' * 40, registration(source, START, end)['implementation_commit'])
        with self.assertRaisesRegex(ValueError, 'does not match'):
            registration(source, START, end + ONE_MINUTE)
        source.write_text(json.dumps(record | {'evaluator_sha256': 'b' * 64}))
        with self.assertRaisesRegex(ValueError, 'does not match'):
            registration(source, START, end)
