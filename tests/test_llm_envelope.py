import json
import unittest
import urllib.error
from unittest import mock

from service.llm import LLMClient, LLMConfig, LLMError


class GatewayEnvelopeTests(unittest.TestCase):
    def test_explicit_success_wrapper_and_plain_completion(self):
        completion={'choices':[{'message':{'content':'{"verdict":"confirm","confidence":0.6}'}}]}
        self.assertEqual(LLMClient._content_of(completion),LLMClient._content_of({'success':True,'data':completion}))

    def test_error_wrapper_is_never_an_answer(self):
        completion={'choices':[{'message':{'content':'pretend answer'}}]}
        for response in ({'success':False,'data':completion}, {'success':'true','data':completion}, {'success':True,'data':[]}):
            with self.assertRaises(LLMError):
                LLMClient._content_of(response)

    def test_reasoning_without_answer_remains_error(self):
        with self.assertRaises(LLMError):
            LLMClient._content_of({'success':True,'data':{'choices':[{'message':{'reasoning_content':'thinking','content':''},'finish_reason':'length'}]}})

    def test_post_fallback_retries_only_502_503(self):
        cfg = LLMConfig(base_url='http://llm.test/v1', model='deepseek-v4.1-flash',
                        api_key='test-key', fallback_models=('gpt-5.5',))
        client = LLMClient(cfg)
        seen = []
        def fake_urlopen(req, timeout, context):
            seen.append(json.loads(req.data.decode())['model'])
            if len(seen) == 1:
                raise urllib.error.HTTPError(req.full_url, 503, 'down', {}, None)
            class Response:
                def __enter__(self): return self
                def __exit__(self, *args): return None
                def read(self): return b'{"choices":[{"message":{"content":"OK"}}]}'
            return Response()
        with mock.patch('service.llm.urllib.request.urlopen', side_effect=fake_urlopen):
            self.assertEqual(client.complete('hello'), 'OK')
        self.assertEqual(seen, ['deepseek-v4.1-flash', 'gpt-5.5'])

    def test_post_fallback_covers_timeout_and_500(self):
        for failure in (TimeoutError('slow upstream'),
                        urllib.error.HTTPError('http://llm.test/v1/chat/completions', 500, 'bad', {}, None)):
            with self.subTest(failure=type(failure).__name__):
                cfg = LLMConfig(base_url='http://llm.test/v1', model='primary',
                                api_key='test-key', fallback_models=('backup',))
                client = LLMClient(cfg)
                seen = []
                def fake_urlopen(req, timeout, context, seen=seen, failure=failure):
                    seen.append(json.loads(req.data.decode())['model'])
                    if len(seen) == 1:
                        raise failure
                    class Response:
                        def __enter__(self): return self
                        def __exit__(self, *args): return None
                        def read(self): return b'{"choices":[{"message":{"content":"OK"}}]}'
                    return Response()
                with mock.patch('service.llm.urllib.request.urlopen', side_effect=fake_urlopen):
                    self.assertEqual(client.complete('hello'), 'OK')
                self.assertEqual(seen, ['primary', 'backup'])
