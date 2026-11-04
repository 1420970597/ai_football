import unittest
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app import hermes_client


class HermesClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_continuation_preserves_session_but_has_distinct_idempotency_without_images(self):
        captured = []
        async def handler(request):
            captured.append(request)
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '核验结果'}}]})
        settings = SimpleNamespace(hermes_api_url='http://hermes.invalid/v1', hermes_api_key='isolated-key',
                                   hermes_model='isolated-model')
        review, attempt = uuid.uuid4(), uuid.uuid4()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.object(hermes_client, '_content_parts', AsyncMock(return_value=[{'type': 'text', 'text': 'sample'},
                              {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,sample'}}])) as parts:
                await hermes_client.review_message(settings, {}, review, attempt, client=client)
                _, summary = await hermes_client.review_message(settings, {}, review, attempt, client=client,
                                                                continuation='继续分页', continuation_round=1)
        self.assertEqual(1, parts.await_count)
        self.assertEqual(captured[0].headers['X-Hermes-Session-Id'], captured[1].headers['X-Hermes-Session-Id'])
        self.assertNotEqual(captured[0].headers['Idempotency-Key'], captured[1].headers['Idempotency-Key'])
        self.assertEqual([{'type': 'text', 'text': '继续分页'}], json.loads(captured[1].content)['messages'][-1]['content'])
        self.assertEqual(0, summary['image_count'])
        self.assertEqual(0, summary['image_bytes'])

    async def test_explicit_provider_error_is_not_returned_as_model_content(self):
        response_body = {
            'model': 'isolated-model',
            'choices': [{'finish_reason': 'error', 'message': {'content': 'temporary upstream detail'}}],
            'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0},
        }

        async def handler(_request):
            return httpx.Response(200, json=response_body)

        settings = SimpleNamespace(hermes_api_url='http://hermes.invalid/v1', hermes_api_key='isolated-key',
                                   hermes_model='isolated-model', hermes_provider='custom:isolated')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.object(hermes_client, '_content_parts', AsyncMock(return_value=[{'type': 'text', 'text': 'sample'}])):
                with self.assertRaises(hermes_client.HermesProviderUnavailable) as raised:
                    await hermes_client.review_message(settings, {}, uuid.uuid4(), uuid.uuid4(), client=client)

        self.assertEqual('Hermes provider returned a transient error', str(raised.exception))
        self.assertNotIn('temporary upstream detail', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
