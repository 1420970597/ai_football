import base64
import gzip
import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from collector.leyu_account import (BetSubmissionRejected, BetSubmissionUnknown, LeyuAccountClient,
                                     _daily_profit, _find_number, _find_records, _net_profit,
                                     _normalise_venue_record, _settlement_time,
                                     _venue_balances)
from collector.session import SessionError


class LeyuAccountTests(unittest.TestCase):
    def bet_detail(self):
        return {'matchId': 'm', 'marketId': 'market1', 'playId': '2',
                'playOptions': 'Over', 'playOptionsId': 'option1', 'oddFinally': '1.95',
                'odds': '195000', 'marketValue': '2.5', 'matchType': 2,
                'sportId': '1', 'scoreBenchmark': ''}

    def test_submission_and_order_query_use_captured_chinese_language(self):
        from collector.leyu_account import VENUE_BET_PATH, VENUE_ORDER_PATH
        client = LeyuAccountClient(SimpleNamespace(app_host='https://offline.invalid'))
        client._acquire_venue_session = lambda: SimpleNamespace(
            request_id='offline-session', host='https://offline.invalid', origin='https://offline.invalid')
        class Response:
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return b'{"code":"0000000","data":{"total":0,"data":[]}}'
        with patch('collector.leyu_account.urllib.request.urlopen', return_value=Response()) as transport:
            for path in (VENUE_BET_PATH, VENUE_ORDER_PATH):
                with self.subTest(path=path):
                    client._venue_request(path, {'playOptions': 'Over'})
                    request = transport.call_args.args[0]
                    headers = {k.lower(): v for k, v in request.header_items()}
                    self.assertEqual(headers['lang'], 'zh')
                    self.assertEqual(headers['accept-language'], 'zh-CN,zh;q=0.9')
                    self.assertEqual(json.loads(request.data)['playOptions'], 'Over')

    def test_order_display_prefers_chinese_aliases_over_english_names(self):
        row = _normalise_venue_record({'detailList': [{
            'matchInfo': 'Home v Away', 'matchNameCn': '主队 v 客队',
            'marketName': 'Full Time Handicap', 'playNameCn': '全场让球',
            'playOptionName': 'Home +0.5', 'playOptionNameCn': '主队 +0.5',
            'playOptions': '1', 'playOptionsId': 'option-1',
        }]}, 'settled')
        self.assertEqual(row['match'], '主队 v 客队')
        self.assertEqual(row['market'], '全场让球')
        self.assertEqual(row['option'], '主队 +0.5')
        self.assertEqual(row['details'][0]['playOptions'], '1')

    def test_bet_preflight_uses_native_queries_and_validates_wallet(self):
        from collector.leyu_account import VENUE_LATEST_MARKET_PATH, VENUE_LIMIT_PATH
        client = LeyuAccountClient(SimpleNamespace(app_host='https://app.invalid'))
        calls = []
        def request(path, body=None):
            calls.append((path, body))
            if path == VENUE_LATEST_MARKET_PATH:
                return [{'id': 'market1', 'matchInfoId': 'm', 'playId': '2', 'matchStatus': 1,
                         'matchHandicapStatus': 0, 'status': 0, 'marketValue': '2.5',
                         'marketOddsList': [{'id': 'option1', 'oddsStatus': 1,
                                             'oddsType': 'Over', 'oddsValue': '195000'}]}]
            if path == VENUE_LIMIT_PATH:
                return [{'code': 0, 'playOptionsId': 'option1', 'playId': '2', 'minBet': '1', 'orderMaxPay': '10'}]
            return {'amount': '20'}
        client._venue_request = request
        fresh = client.prepare_bet(self.bet_detail(), 2)
        self.assertEqual(fresh['oddFinally'], '1.95')
        self.assertEqual(calls[0][1]['idList'][0]['oddsId'], 'option1')
        self.assertEqual(calls[1][1]['orderMaxBetMoney'][0]['playOptionId'], 'option1')
        self.assertEqual(set(calls[1][1]['orderMaxBetMoney'][0]), {
            'deviceType', 'marketId', 'matchId', 'matchType', 'oddsValue', 'playId', 'playOptionId'})
        with self.assertRaisesRegex(SessionError, '限额'):
            client.prepare_bet(self.bet_detail(), 100)
        changed = self.bet_detail()
        changed['oddFinally'] = '1.94'
        with self.assertRaisesRegex(SessionError, '赔率已变化'):
            client.prepare_bet(changed, 2)

    def test_captured_limit_blank_echoes_pass_and_invalid_limits_stop_before_wallet(self):
        from collector.leyu_account import VENUE_LATEST_MARKET_PATH, VENUE_LIMIT_PATH, VENUE_AMOUNT_PATH
        client = LeyuAccountClient(SimpleNamespace(app_host='https://offline.invalid'))
        market = {'id': 'market1', 'matchInfoId': 'm', 'playId': 2, 'matchStatus': 1,
                  'matchHandicapStatus': 0, 'status': 0, 'marketValue': '2.5',
                  'marketOddsList': [{'id': 'option1', 'oddsStatus': 1,
                                      'oddsType': 'Over', 'oddsValue': 195000}]}
        # Sanitized session 18 schema; only option identifiers are replaced.
        limit = {'code': 0, 'minBet': '2', 'orderMaxPay': '60000',
                 'playId': '', 'playOptionsId': 'option1', 'type': ''}
        calls = []
        response = [limit]

        def request(path, body=None):
            calls.append(path)
            if path == VENUE_LATEST_MARKET_PATH:
                return [market]
            if path == VENUE_LIMIT_PATH:
                return response
            if path == VENUE_AMOUNT_PATH:
                return {'amount': 111.86}
            self.fail('unexpected endpoint: ' + path)

        client._venue_request = request
        self.assertEqual(client.prepare_bet(self.bet_detail(), 2)['oddFinally'], '1.95')
        self.assertEqual(calls[-1], VENUE_AMOUNT_PATH)
        for change in ({'playId': 'other'}, {'type': '2'}, {'code': '0400469'},
                       {'code': None}, {'playOptionsId': 'other'}, {'minBet': '3'},
                       {'minBet': 'nan'}, {'orderMaxPay': 'inf'}, {'orderMaxPay': '0'}):
            with self.subTest(change=change):
                response = [{**limit, **change}]
                calls.clear()
                with self.assertRaises(SessionError):
                    client.prepare_bet(self.bet_detail(), 2)
                self.assertNotIn(VENUE_AMOUNT_PATH, calls)
        for invalid_response in ([], [limit, dict(limit)], [{k: v for k, v in limit.items() if k != 'code'}]):
            response = invalid_response
            calls.clear()
            with self.assertRaises(SessionError):
                client.prepare_bet(self.bet_detail(), 2)
            self.assertNotIn(VENUE_AMOUNT_PATH, calls)

    def test_bet_preflight_fails_closed_on_closed_or_missing_market(self):
        client = LeyuAccountClient(SimpleNamespace(app_host='https://app.invalid'))
        for response in ([], None, [{'id': 'other'}], [{'id': 'market1', 'matchInfoId': 'm',
                                                        'playId': '2', 'matchStatus': 3}]):
            with self.subTest(response=response), self.assertRaises(SessionError):
                client._venue_request = lambda *_a, response=response, **_k: response
                client.prepare_bet(self.bet_detail(), 2)

    def test_bet_receipts_require_explicit_code_order_number_and_status(self):
        client = LeyuAccountClient(SimpleNamespace(app_host='https://app.invalid'))
        for code, expected in ((1, 'accepted'), (2, 'pending')):
            client._venue_request = lambda *_a, code=code: {'code': '0000000', 'data': {
                'orderDetailRespList': [{'orderNo': 'ORDER1', 'orderStatusCode': code}]}}
            result = client.submit_bet({})
            self.assertEqual(result['status'], expected)
            self.assertTrue(result['submitted'])
        for response in ({'code': '0400469'}, {'code': '0000000', 'data': {
                'orderDetailRespList': [{'orderStatusCode': 0}]}}):
            client._venue_request = lambda *_a, response=response: response
            with self.assertRaises(BetSubmissionRejected):
                client.submit_bet({})
        for response in ({}, {'code': '0000000'}, {'code': '0000000', 'data': {
                'orderDetailRespList': [{'orderStatusCode': 1}]}}):
            client._venue_request = lambda *_a, response=response: response
            with self.assertRaises(BetSubmissionUnknown):
                client.submit_bet({})

    def test_bet_http_and_business_auth_failures_do_not_retry(self):
        from unittest.mock import patch
        from collector.leyu_account import VENUE_BET_PATH
        import urllib.error
        client = LeyuAccountClient(SimpleNamespace(app_host='https://app.invalid'))
        client._acquire_venue_session = lambda: SimpleNamespace(
            request_id='test-session', host='https://api.invalid', origin='https://h5.invalid')
        for exc in (urllib.error.HTTPError('https://api.invalid', 401, 'expired', {}, None), TimeoutError()):
            with patch('collector.leyu_account.urllib.request.urlopen', side_effect=exc) as send:
                with self.assertRaises(BetSubmissionUnknown):
                    client.submit_bet({})
                send.assert_called_once()
        class Response:
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *_a): return False
            def read(self): return b'{"code":"0401013"}'
        with patch('collector.leyu_account.urllib.request.urlopen', return_value=Response()) as send:
            with self.assertRaises(BetSubmissionRejected):
                client.submit_bet({})
            self.assertEqual(send.call_args.args[0].full_url, 'https://api.invalid' + VENUE_BET_PATH)
            send.assert_called_once()

    def test_normalises_native_venue_order(self):
        row = _normalise_venue_record({
            'orderNo': 'O-1', 'matchName': '切尔西 v 伯恩茅斯',
            'marketName': '全场独赢', 'playOptionsName': '切尔西',
            'oddFinally': '1.69', 'betAmount': '2.00', 'scoreBenchmark': '0:0',
        }, 'unsettled')
        self.assertEqual(row['order_no'], 'O-1')
        self.assertEqual(row['market'], '全场独赢')
        self.assertEqual(row['option'], '切尔西')
        self.assertEqual(row['odds'], 1.69)
        self.assertEqual(row['amount'], 2.0)
        self.assertEqual(row['status'], 'unsettled')

    def test_native_order_envelope_is_decoded_and_counted(self):
        client = LeyuAccountClient(SimpleNamespace(app_host='https://app.invalid'))
        envelope = {'code': '0000000', 'data': base64.b64encode(gzip.compress(json.dumps({
            'total': 1,
            'data': [{'orderNo': 'O-1', 'marketName': '全场独赢', 'oddFinally': '1.69'}],
        }).encode())).decode()}
        client._venue_request = lambda path, body=None, decode=False: (
            __import__('collector.leyu_client', fromlist=['decode_envelope']).decode_envelope(envelope)
        )
        result = client._fetch_venue_records(0)
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['items'][0]['order_no'], 'O-1')
        self.assertEqual(result['items'][0]['odds'], 1.69)
        self.assertEqual(result['source'], 'leyu_ybty')

    def test_native_request_decompresses_http_gzip(self):
        client = LeyuAccountClient(SimpleNamespace(app_host='https://app.invalid'))
        client._acquire_venue_session = lambda: SimpleNamespace(
            request_id='RID', host='https://api.invalid', origin='https://h5.invalid', expired=False)
        import collector.leyu_account as account
        class Response:
            headers = {'Content-Encoding': 'gzip'}
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return gzip.compress(json.dumps({'amount': '111.86'}).encode())
        original = account.urllib.request.urlopen
        account.urllib.request.urlopen = lambda *args, **kwargs: Response()
        try:
            self.assertEqual(client._venue_request('/yewu12/api/user/amount')['amount'], '111.86')
        finally:
            account.urllib.request.urlopen = original

    def test_native_request_reacquires_session_once_after_http_auth_error(self):
        client = LeyuAccountClient(SimpleNamespace(app_host='https://app.invalid'))
        session = SimpleNamespace(request_id='RID', host='https://api.invalid',
                                  origin='https://h5.invalid', expired=False)
        acquired = []
        client._acquire_venue_session = lambda: (acquired.append(1) or session)
        import collector.leyu_account as account
        calls = []
        class Response:
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return b'{"amount":"111.86"}'
        def urlopen(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise account.urllib.error.HTTPError('https://api.invalid', 401, 'expired', {}, None)
            return Response()
        original = account.urllib.request.urlopen
        account.urllib.request.urlopen = urlopen
        try:
            result = client._venue_request('/yewu12/api/user/amount')
            self.assertEqual(result['amount'], '111.86')
            self.assertEqual(len(calls), 2)
        finally:
            account.urllib.request.urlopen = original

    def test_native_account_data_wins_over_centre_wallet_fallback(self):
        client = LeyuAccountClient(SimpleNamespace(credentials=SimpleNamespace(token='OLD', uuid='UUID'),
            app_host='https://example.invalid', signer=None, signature='SIGN'))
        client._post = lambda path, body: ({
            'status_code': 6000, 'data': {'centerWalletBalance': '0'}
        } if path.endswith('getBalance') else {
            'status_code': 6000, 'data': [{'enName': 'YBTY', 'amount': '111.86'}]
        })
        client._fetch_venue_account = lambda: {
            'source': 'leyu_ybty', 'sports_balance': 111.86,
            'unsettled': {'count': 1, 'items': [{'order_no': 'O-1'}]},
            'settled': {'count': 0, 'items': []},
        }
        result = client.fetch()
        self.assertEqual(result['source'], 'leyu_ybty')
        self.assertEqual(result['sports_balance'], 111.86)
        self.assertEqual(result['unsettled']['count'], 1)
        self.assertEqual(result['unsettled']['items'][0]['order_no'], 'O-1')

    def test_extracts_balance_and_nested_record_list(self):
        self.assertEqual(_find_number({'data': {'balance': '12.50'}}, {'balance'}), 12.5)
        self.assertEqual(_find_records({'data': {'list': [{'id': 'a'}]}}), [{'id': 'a'}])
        self.assertEqual(_find_records({'data': {'gameRecordList': [
            {'day': '2026-10-09', 'data': [{'id': 'a'}, {'id': 'b'}]}
        ]}}), [{'id': 'a', 'day': '2026-10-09'}, {'id': 'b', 'day': '2026-10-09'}])
        self.assertEqual(_venue_balances({'data': [
            {'enName': 'YBTY', 'amount': '111.86'}
        ]})[0]['enName'], 'YBTY')

    def test_record_query_matches_native_filter(self):
        query = LeyuAccountClient._record_query(0)
        self.assertEqual(query['flag'], 0)
        self.assertEqual(query['page'], 1)
        self.assertEqual(query['pageSize'], 20)
        self.assertEqual(query['queryFlag'], 1)
        self.assertEqual(query['venueId'], 0)
        self.assertEqual(query['gameType'], 1)
        self.assertEqual(query['nationalityCode'], 'CN')
        self.assertTrue(query['startAt'].endswith('00:00:00'))
        self.assertTrue(query['endAt'].endswith('23:59:59'))

    def test_fetch_keeps_unavailable_account_explicit(self):
        client = LeyuAccountClient(SimpleNamespace(
            credentials=SimpleNamespace(token='OLD', uuid='UUID'), app_host='https://example.invalid',
            signer=None, signature='SIGN'))

        calls = []
        def post(path, body):
            calls.append((path, dict(body)))
            if path.endswith('getBalance'):
                return {'status_code': 6000, 'data': {}}
            if path.endswith('allBalance'):
                return {'status_code': 6000, 'data': [{'enName': 'YBTY', 'amount': '11.5'}]}
            if path.endswith('betRecordTotal'):
                if body['flag'] == 0:
                    raise SessionError('接口不可用')
                return {'status_code': 6000, 'data': {'totalLine': {'countId': 2}}}
            return {'status_code': 6000, 'data': {'gameRecordList': [
                {'day': '2026-10-09', 'data': [{'id': 'x'}]}]}}

        client._post = post
        result = client.fetch()
        self.assertTrue(result['available'])
        self.assertEqual(result['balance'], 11.5)
        self.assertEqual(result['sports_balance'], 11.5)
        self.assertEqual(result['unsettled']['items'][0]['id'], 'x')
        self.assertEqual(result['unsettled']['items'][0]['status'], 'unsettled')
        self.assertEqual(result['settled']['count'], 2)
        self.assertIn('接口不可用', result['error'])
        record_calls = [(path, body) for path, body in calls if 'betRecord' in path]
        self.assertEqual(len(record_calls), 4)
        self.assertEqual({body['flag'] for _, body in record_calls}, {0, 1})
        self.assertTrue(all(body['startAt'] and body['endAt'] for _, body in record_calls))

    def test_fetch_propagates_balance_failure(self):
        client = LeyuAccountClient(SimpleNamespace(credentials=SimpleNamespace(token='OLD')))
        client._post = lambda path, body: (_ for _ in ()).throw(SessionError('余额接口失败'))
        with self.assertRaises(SessionError):
            client.fetch()

    def test_expired_account_token_refreshes_once(self):
        client = LeyuAccountClient(SimpleNamespace(credentials=SimpleNamespace(token='OLD')))
        refreshed = type('Provider', (), {'refresh_token': lambda self: 'NEW'})()
        client.login_provider = refreshed
        self.assertTrue(client._refresh_token())
        self.assertEqual(client.bootstrapper.credentials.token, 'NEW')

    def test_business_error_preserves_server_message(self):
        client = LeyuAccountClient(SimpleNamespace(
            credentials=SimpleNamespace(token='OLD', uuid='UUID'), app_host='https://example.invalid',
            signer=None, signature='SIGN'))

        # Call the real request response validator without making a network
        # request by replacing urllib at the narrowest boundary.
        import collector.leyu_account as account
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return '{"status_code":6008,"message":"时间格式不正确，请选择查询时间"}'.encode('utf-8')
        original = account.urllib.request.urlopen
        account.urllib.request.urlopen = lambda *args, **kwargs: Response()
        try:
            with self.assertRaisesRegex(SessionError, '6008.*时间格式不正确'):
                client._post('/game/api/v1/record/betRecordTotal', {})
        finally:
            account.urllib.request.urlopen = original


class LeyuDailyProfitTests(unittest.TestCase):
    at = datetime.fromisoformat('2026-10-10T12:00:00+08:00').timestamp()

    def row(self, order='test-order', profit='0.82', settled_at='2026-10-10T09:00:00+08:00', **changes):
        return {'order_no': order, 'status': 'settled', 'profitAmount': profit,
                'settleTime': settled_at, **changes}

    def page(self, rows, count=None, more=False):
        return {'source': 'leyu_ybty', 'count': len(rows) if count is None else count,
                'total_known': True, 'items': rows, 'has_more': more}

    def client(self):
        return LeyuAccountClient(SimpleNamespace(app_host='https://offline.invalid'))

    def test_net_profit_uses_native_net_including_zero_and_loss(self):
        for value in ('0.82', 0, '-2'):
            with self.subTest(value=value):
                self.assertEqual(_net_profit({'profitAmount': value, 'backAmount': 99, 'amount': 2}), float(value))

    def test_gross_payout_subtracts_stake_and_does_not_guess_win_amount(self):
        for returned, expected in (('2.82', .82), (0, -2), (2, 0)):
            with self.subTest(returned=returned):
                row = _normalise_venue_record({'backAmount': returned, 'orderAmountTotal': 2}, 'settled')
                self.assertEqual(row['profit'], expected)
        self.assertEqual(_net_profit({'payout': 3, 'stake': 2}), 1)
        self.assertIsNone(_net_profit({'winAmount': 3, 'amount': 2}))
        self.assertIsNone(_net_profit({'backAmount': 3}))

    def test_invalid_net_profit_cannot_be_replaced_with_gross_payout(self):
        for value in ('nan', 'inf', '-inf', True, 'invalid', 10 ** 1000):
            with self.subTest(value=str(value)[:20]):
                self.assertIsNone(_net_profit({'profitAmount': value, 'backAmount': 3, 'amount': 2}))

    def test_settlement_time_supports_native_milliseconds_seconds_and_iso(self):
        expected = datetime.fromisoformat('2026-10-10T09:00:00+08:00')
        for value in (expected.timestamp(), str(int(expected.timestamp() * 1000)),
                      '2026-10-10T01:00:00Z', '2026-10-10 09:00:00'):
            with self.subTest(value=value):
                self.assertEqual(_settlement_time({'settleTime': value}), expected)
        self.assertEqual(_settlement_time({'settled_at': expected.isoformat()}), expected)
        for value in (None, '', 0, -1, 'nan', 'inf', True, 'bad', 10 ** 1000):
            with self.subTest(value=str(value)[:20]):
                self.assertIsNone(_settlement_time({'settleTime': value}))

    def test_beijing_day_uses_settlement_time_even_for_older_bets(self):
        rows = [self.row('today', '1.20', '2026-10-09T16:00:00Z', betTime='2026-10-08'),
                self.row('yesterday', 100, '2026-10-09T15:59:59Z'),
                self.row('tomorrow', 100, '2026-10-10T16:00:00Z'),
                self.row('loss', -2), self.row('refund', 0)]
        result = _daily_profit(rows, self.at)
        self.assertTrue(result['available'])
        self.assertEqual(result['amount'], -.8)
        self.assertEqual(result['settled_count'], 3)
        self.assertEqual(result['date'], '2026-10-10')
        self.assertEqual(result['timezone'], 'Asia/Shanghai')
        self.assertEqual(result['basis'], 'settlement_time')

    def test_empty_day_and_unsettled_orders_do_not_create_losses(self):
        rows = [self.row('pending', -20, status='unsettled'), self.row('rejected', -20, status='rejected')]
        for items in ([], rows):
            result = _daily_profit(items, self.at)
            self.assertTrue(result['available'])
            self.assertEqual(result['amount'], 0)
            self.assertEqual(result['settled_count'], 0)

    def test_missing_fields_and_incomplete_history_are_unavailable(self):
        for row in (self.row(settleTime=None, betTime=self.at * 1000),
                    self.row(profit=None), self.row(profit='nan')):
            result = _daily_profit([row], self.at)
            self.assertFalse(result['available'])
            self.assertIsNone(result['amount'])
            self.assertTrue(result['reason'])
        self.assertFalse(_daily_profit([], self.at, complete=False)['available'])
        # A known older settlement's missing amount cannot affect today's sum.
        self.assertTrue(_daily_profit([self.row(profit=None, settled_at='2026-10-09')], self.at)['available'])

    def test_duplicate_order_ids_are_counted_once_and_money_sums_exactly(self):
        row = self.row(profit='.1')
        result = _daily_profit([row, dict(row), self.row('second', '.2')], self.at)
        self.assertEqual(result['amount'], .3)
        self.assertEqual(result['settled_count'], 2)

    def test_oversized_aggregate_is_unavailable_instead_of_crashing(self):
        self.assertFalse(_daily_profit([self.row(profit=1e100)], self.at)['available'])

    def test_native_order_query_sends_page_and_preserves_unknown_total(self):
        client = self.client()
        with patch.object(client, '_venue_request', return_value={'total': 101, 'data': []}) as request:
            result = client._fetch_venue_records(1, page=2)
            self.assertEqual(request.call_args.args[1]['page'], 2)
            self.assertEqual(request.call_args.args[1]['size'], 100)
            self.assertTrue(result['total_known'])
            self.assertFalse(result['has_more'])
        with patch.object(client, '_venue_request', return_value={'data': []}):
            self.assertFalse(client._fetch_venue_records(1)['total_known'])

    def test_pagination_includes_today_settlement_after_first_hundred_orders(self):
        client = self.client()
        first = self.page([self.row(str(i), 1, '2026-10-09') for i in range(100)], 101, True)
        last = self.page([self.row('last', '1.25', betTime='2026-10-08')], 101)
        with patch.object(client, '_fetch_venue_records', return_value=last) as query:
            result = client._fetch_today_pnl(first, self.at)
            query.assert_called_once_with(1, page=2)
        self.assertTrue(result['available'])
        self.assertEqual(result['amount'], 1.25)
        self.assertEqual(result['settled_count'], 1)

    def test_pagination_rejects_short_duplicate_missing_ids_or_changed_total(self):
        client = self.client()
        first = self.page([self.row('first')], 2, True)
        for last in (self.page([], 2), self.page([self.row('first')], 2),
                     self.page([self.row(order=None)], 2), self.page([self.row('second')], 3),
                     {**self.page([self.row('second')], 2), 'total_known': False}):
            with self.subTest(last=last), patch.object(client, '_fetch_venue_records', return_value=last):
                self.assertFalse(client._fetch_today_pnl(first, self.at)['available'])
        self.assertFalse(client._fetch_today_pnl(self.page([self.row()], 2), self.at)['available'])
        self.assertFalse(client._fetch_today_pnl(self.page([self.row(), self.row()], 2), self.at)['available'])

    def test_pagination_failure_and_page_limit_are_unavailable(self):
        client = self.client()
        first = self.page([self.row('first')], 3, True)
        with patch.object(client, '_fetch_venue_records', side_effect=SessionError('测试超时')):
            self.assertFalse(client._fetch_today_pnl(first, self.at)['available'])
        with patch('collector.leyu_account.ACCOUNT_PNL_MAX_PAGES', 2), patch.object(
                client, '_fetch_venue_records', return_value=self.page([self.row('second')], 3, True)) as query:
            self.assertFalse(client._fetch_today_pnl(first, self.at)['available'])
            query.assert_called_once()

    def test_native_empty_history_is_zero_but_unknown_or_legacy_is_unavailable(self):
        client = self.client()
        self.assertEqual(client._fetch_today_pnl(self.page([]), self.at)['amount'], 0)
        for change in ({'source': 'leyu_app'}, {'total_known': False}, {'count': None}, {'count': -1}):
            self.assertFalse(client._fetch_today_pnl({**self.page([]), **change}, self.at)['available'])

    def test_account_response_includes_daily_net_profit(self):
        client = self.client()
        with patch.object(client, '_post', return_value={'data': {'balance': 10}}), patch.object(
                client, '_fetch_venue_account', return_value={'sports_balance': 10,
                    'settled': self.page([self.row()]), 'unsettled': self.page([])}), patch(
                'collector.leyu_account.time.time', return_value=self.at):
            result = client.fetch()
        self.assertEqual(result['today_pnl']['amount'], .82)
        self.assertEqual(result['today_pnl']['settled_count'], 1)


if __name__ == '__main__':
    unittest.main()
