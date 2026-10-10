import base64
import gzip
import json
import unittest
from types import SimpleNamespace

from collector.leyu_account import (BetSubmissionRejected, BetSubmissionUnknown, LeyuAccountClient,
                                     _find_number, _find_records, _normalise_venue_record,
                                     _venue_balances)
from collector.session import SessionError


class LeyuAccountTests(unittest.TestCase):
    def bet_detail(self):
        return {'matchId': 'm', 'marketId': 'market1', 'playId': '2',
                'playOptions': 'Over', 'playOptionsId': 'option1', 'oddFinally': '1.95',
                'odds': '195000', 'marketValue': '2.5', 'matchType': 2,
                'sportId': '1', 'scoreBenchmark': ''}

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
                return [{'playOptionsId': 'option1', 'playId': '2', 'minBet': '1', 'orderMaxPay': '10'}]
            return {'amount': '20'}
        client._venue_request = request
        fresh = client.prepare_bet(self.bet_detail(), 2)
        self.assertEqual(fresh['oddFinally'], '1.95')
        self.assertEqual(calls[0][1]['idList'][0]['oddsId'], 'option1')
        self.assertEqual(calls[1][1]['orderMaxBetMoney'][0]['playOptionId'], 'option1')
        with self.assertRaisesRegex(SessionError, '限额'):
            client.prepare_bet(self.bet_detail(), 100)
        changed = self.bet_detail()
        changed['oddFinally'] = '1.94'
        with self.assertRaisesRegex(SessionError, '赔率已变化'):
            client.prepare_bet(changed, 2)

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


if __name__ == '__main__':
    unittest.main()
