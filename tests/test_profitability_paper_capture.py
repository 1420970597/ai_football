from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from infra.profitability.paper_capture import PaperQuoteClient, capture
from infra.profitability.paper_capture_candidates import candidate
from infra.profitability.paper_price import assess
from infra.profitability.paper_settle import settle_paper
from services.collector.leyu_protocol import LeyuError
from services.executor.account_protocol import AMOUNT_PATH, BET_PATH, LATEST_MARKET_PATH, LIMITS_PATH, AccountProtocolClient


def recommendation(now):
    return {
        'signal_id': str(uuid4()), 'fixture_id': 'leyu:1', 'expert_id': str(uuid4()),
        'created_at': now - timedelta(seconds=45), 'posted_at': now - timedelta(seconds=55),
        'sport': 'football', 'phase': 'live', 'current_phase': 'live', 'market_kind': 'total',
        'market_id': '2', 'period': 'full_time', 'line': '2.5', 'selection': 'over',
        'score_basis': 'full_period', 'score_baseline': '{"home":1,"away":0,"half_home":null,"half_away":null}',
        'recommendation_price': '2', 'quote_time': now - timedelta(seconds=49),
        'home_score': 1, 'away_score': 0, 'half_home_score': None, 'half_away_score': None,
        'stale_projection_at': None, 'decision_context_only_at': None,
        'current_trust': True, 'superseded': False, 'withdrawn_at': None, 'deleted_at': None,
        'media_state': 'ready', 'outcome': 'pending', 'snapshot': '{"market_instance_id":"market","play_id":"2"}',
        'options': json.dumps([
            {'selection': side, 'selection_id': side, 'price': '1.91',
             'received_at': now.isoformat(), 'source_updated_at': now.isoformat()} for side in ('over', 'under')
        ]),
    }


class PaperCaptureTest(unittest.IsolatedAsyncioTestCase):
    def test_candidate_preserves_the_signal_and_rejects_unverified_contract_context(self):
        now = datetime.now(timezone.utc)
        row = recommendation(now)
        result = candidate(row, now)
        self.assertEqual('candidate', result['status'])
        self.assertEqual({'Under': 'under'}, result['companions'])
        self.assertEqual('1:0', result['native_selection']['score_benchmark'])
        for updates, reason in (
            ({'home_score': 2}, 'score_changed_or_unverified'),
            ({'superseded': True}, 'source_ineligible'),
            ({'posted_at': now - timedelta(minutes=6)}, 'source_deadline'),
            ({'options': None}, 'current_book_missing_or_incomplete'),
            ({'current_phase': 'ended'}, 'fixture_not_open'),
            ({'fixture_id': 'other:1'}, 'unsupported_contract'),
        ):
            with self.subTest(reason=reason):
                result = candidate(row | updates, now)
                self.assertEqual(reason, result['status'])
                self.assertEqual(row['signal_id'], result['signal_id'])

    def frame(self):
        observed = datetime.now(timezone.utc)
        slot = observed.replace(second=0, microsecond=0)
        return {
            'slot': slot.isoformat(), 'observed_at': observed.isoformat(), 'capture_mode': 'paper_preflight',
            'begin': (slot - timedelta(seconds=90)).isoformat(), 'end': (slot - timedelta(seconds=30)).isoformat(),
            'rows': [candidate(recommendation(observed), observed) for _ in range(2)],
            'transport': {'scope': {'user_id': str(uuid4()), 'account_id': str(uuid4()), 'revision': 1},
                          'base_url': 'https://book.invalid', 'origin': 'https://app.invalid',
                          'request_id': 'session-probe-dummy', 'amount': '5'},
        }

    def responses(self, *, available='100', selection_id='over'):
        latest = [{
            'id': 'market', 'matchInfoId': '1', 'playId': '2', 'marketValue': '2.5',
            'matchStatus': 1, 'matchOver': 0, 'status': 0, 'matchHandicapStatus': 0,
            'marketOddsList': [
                {'id': selection_id, 'oddsType': 'Over', 'oddsStatus': 1, 'oddsValue': '193000'},
                {'id': 'under', 'oddsType': 'Under', 'oddsStatus': 1, 'oddsValue': '194000'},
            ],
        }]
        return {LATEST_MARKET_PATH: latest, AMOUNT_PATH: {'amount': available},
                LIMITS_PATH: [{'code': 0, 'playOptionsId': selection_id, 'minBet': '1', 'orderMaxPay': '10'}]}

    async def test_real_protocol_reads_quote_and_limits_once_without_keeping_account_material(self):
        responses = self.responses()
        with patch.object(AccountProtocolClient, '_request', AsyncMock(side_effect=lambda path, body: responses[path])) as request:
            document = await capture(self.frame())
        self.assertEqual([LATEST_MARKET_PATH, LIMITS_PATH, AMOUNT_PATH], [call.args[0] for call in request.await_args_list])
        self.assertEqual(['native_preflight_available', 'paper_slot_budget'],
                         [row['status'] for row in document['rows']])
        self.assertEqual('1.93', document['rows'][0]['native_selected_price'])
        self.assertEqual('5', document['rows'][0]['paper_amount_passing_native_limits'])
        self.assertNotIn('transport', document)
        self.assertNotIn('native_selection', document['rows'][0])
        self.assertNotIn('request_id', str(document))

    async def test_limits_and_replaced_option_never_produce_a_modelled_fill(self):
        for responses, status in ((self.responses(available='1'), 'native_preflight_unavailable'),
                                  (self.responses(selection_id='replaced'), 'native_selection_changed')):
            with self.subTest(status=status):
                with patch.object(AccountProtocolClient, '_request',
                                  AsyncMock(side_effect=lambda path, body: responses[path])):
                    result = await capture(self.frame())
                row = result['rows'][0]
                self.assertEqual(status, row['status'])
                self.assertNotIn('native_selected_price', row)

    async def test_guard_refuses_any_order_submission_before_transport_is_called(self):
        frame = self.frame()
        from services.executor.account_session import AccountScope, VenueSession

        scope = AccountScope(**frame['transport']['scope'])
        session = VenueSession(scope, 'https://book.invalid', 'dummy', 'https://app.invalid')
        async with PaperQuoteClient(scope, session, live_orders_enabled=False) as client:
            with patch.object(AccountProtocolClient, '_request', AsyncMock()) as request:
                for path, submit in ((BET_PATH, True), (BET_PATH, False), (LATEST_MARKET_PATH, True)):
                    with self.assertRaisesRegex(LeyuError, 'paper_read_path_forbidden'):
                        await client._request(path, submission=submit)
                request.assert_not_awaited()

    async def test_transport_unavailable_leaves_a_visible_opportunity(self):
        frame = self.frame()
        del frame['transport']
        document = await capture(frame)
        self.assertEqual(['paper_account_unavailable'] * 2, [row['status'] for row in document['rows']])

    def test_reprice_at_observed_offer_with_versioned_quarter_line_settlement(self):
        now = datetime.now(timezone.utc)
        row = recommendation(now)
        offer = candidate(row | {'line': '2.75'}, now) | {
            'native_quote_checked_at': now.isoformat(), 'native_selected_price': '1.9',
        }
        calculation = {
            'signal': {'sport': 'football', 'kind': 'total', 'period': 'full_time', 'phase': 'live',
                       'price': '2.00', 'selection': 'over', 'score_basis': 'full_period',
                       'score_baseline': json.loads(row['score_baseline']), 'line': '2.750'},
            'result': {'status': 'confirmed', 'home': 2, 'away': 1},
        }
        ledger = {'fixture_id': offer['fixture_id'], 'version': 2, 'result_status': 'confirmed',
                  'received_at': now + timedelta(minutes=20), 'computed_at': now + timedelta(minutes=21),
                  'calculation': json.dumps(calculation), 'outcome': 'half_win',
                  'unit_net': Decimal('0.5'), 'effective_stake': Decimal('0.5')}
        result = settle_paper(offer, ledger)
        self.assertEqual('half_win', result['outcome'])
        self.assertEqual(Decimal('0.45'), Decimal(result['quoted_unit_return']))
        self.assertEqual(Decimal('0.441'), Decimal(result['stressed_unit_return_before_unknown_costs']))
        self.assertEqual(2, result['result_version'])
        self.assertFalse(result['validation_eligible'])
        self.assertEqual('result_timing_unverified',
                         settle_paper(offer, ledger | {'received_at': now})['status'])
        self.assertEqual('result_contract_mismatch', settle_paper(offer | {'selection': 'under'}, ledger)['status'])
        self.assertEqual('stored_settlement_not_reproduced',
                         settle_paper(offer, ledger | {'unit_net': Decimal('1')})['status'])

    async def test_independent_reference_checks_use_contemporaneous_native_offer(self):
        now = datetime.now(timezone.utc)
        offer = candidate(recommendation(now), now) | {
            'native_quote_checked_at': now.isoformat(), 'native_selected_price': '1.93',
        }
        inquire = AsyncMock(return_value={'status': 'reference_ineligible',
                                          'reasons': ['settlement_and_cost_approval_missing']})
        result = await assess(offer, '/fake/socket', inquire, now=now)
        self.assertEqual('reference_ineligible', result['status'])
        _, row, price, checked, cutoff = inquire.await_args.args
        self.assertEqual(('total', 1, 0, Decimal('1.93'), now, now),
                         (row['current_kind'], row['home_score'], row['away_score'], price, checked, cutoff))
        inquire.reset_mock()
        result = await assess(offer, '/fake/socket', inquire, now=now + timedelta(seconds=6))
        self.assertEqual({'status': 'reference_cutoff_expired'}, result)
        inquire.assert_not_awaited()
