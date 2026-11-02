import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app import hermes_client


class HermesClientTests(unittest.IsolatedAsyncioTestCase):
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
