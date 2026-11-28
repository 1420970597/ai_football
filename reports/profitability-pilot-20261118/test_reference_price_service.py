from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from reference_price_service import price_offer


class ReferencePriceServiceTests(unittest.TestCase):
    def test_consumes_only_timely_complete_approved_books(self):
        cutoff = '2026-11-19T11:59:25+00:00'
        contract = {'fixture_id': 'leyu:123', 'market_id': '2', 'sport': 'football',
                    'phase': 'scheduled', 'kind': 'total', 'period': 'full_time', 'line': '2.5',
                    'score_basis': 'full_period', 'settlement_scope': 'regulation'}
        offer = contract | {'selection': 'over', 'effective_score_basis': 'full_period', 'price': '1.80',
                            'provider_quote_checked_at': '2026-11-19T11:59:23+00:00', 'captured_at': cutoff}
        book = {'bid': '0.59', 'ask': '0.61', 'bid_size': '13', 'ask_size': '16', 'status': 'two_sided',
                'quote_time': '2026-11-19T11:59:20+00:00', 'received_at': '2026-11-19T11:59:22+00:00'}
        market = {'market': 'total', 'rules_sha256': 'digest'}
        polymarket = market | {'source': 'polymarket', 'accepting_orders': True, 'books': [
            book | {'outcome': 'Over'}, book | {'outcome': 'Under', 'bid': '0.39', 'ask': '0.41'},
        ]}
        kalshi = market | {'source': 'kalshi', 'status': 'active', 'books': [book | {'outcome': 'yes'}]}
        approval = {'contract': contract, 'approved_at': '2026-11-19T11:30:00+00:00',
                    'cost_per_unit': '0.01', 'cost_evidence': 'cost', 'settlement_evidence': 'rules',
                    'required_net_edge': '0.02', 'offer_feed_origin': 'leyu',
                    'sources': {source: {'venue': source, 'feed_origin': source, 'minimum_book_depth': '12',
                                         'rules': {'total': 'digest'}} for source in ('polymarket', 'kalshi')}}
        fixture = {
            'label': 'match', 'approvals': [approval],
            'bindings': [contract | market | {'source': source} for source in ('polymarket', 'kalshi')],
            'outcomes': {
                'polymarket': {'total': market | {'labels': {'Over': 'over', 'Under': 'under'}}},
                'kalshi': {'total': market | {'labels': {'yes': 'over'}}},
            },
        }
        now = datetime.fromisoformat(cutoff)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'reference-price.json').write_text(json.dumps({'fixtures': {'leyu:123': fixture}}))
            (root / 'books').mkdir()
            archive = root / 'books' / 'match-2026-11-19.jsonl'
            batch = {'captured_at': cutoff, 'markets': [polymarket, kalshi]}
            archive.write_text(json.dumps(batch) + '\n')
            eligible = price_offer(offer, root, now)
            self.assertEqual('price_eligible', eligible['status'])
            self.assertEqual(Decimal('1.03') / Decimal('0.59'), Decimal(eligible['pricing']['minimum_odds']))
            self.assertEqual('below_minimum_odds', price_offer(offer | {'price': '1.74'}, root, now)['status'])
            kalshi['books'][0]['quote_time'] = None
            archive.write_text(json.dumps(batch) + '\n')
            self.assertEqual('ineligible', price_offer(offer, root, now)['status'])
            (root / 'reference-price.json').write_text(json.dumps(
                {'fixtures': {'leyu:123': fixture | {'approvals': []}}},
            ))
            self.assertIn('settlement_and_cost_approval_missing', price_offer(offer, root, now)['reasons'])
