import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

import httpx
from fastapi import HTTPException

from app import hermes_client
from app.runtime_settings import runtime_configuration, runtime_quote_max_age, validate_runtime_value
from app.security import encrypt_secret


class RuntimeSettingsTests(unittest.IsolatedAsyncioTestCase):
    def settings(self):
        return SimpleNamespace(encryption_key=b'k'*32, quote_max_age_seconds=15,
            hermes_model='hermes-agent', hermes_provider='custom:cline',
            review_config_url='http://api:8080/internal/review/config',
            hermes_api_url='http://hermes:8642/v1', hermes_api_key='internal-test-key')

    def test_runtime_input_rejects_invalid_threshold_and_model(self):
        for value in ('0', '3601', '1.5', 'true', '-1', '１２'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_runtime_value('quote_max_age_seconds', value)
        for value in ('https://provider.invalid', 'x\nAuthorization:foo', 'hermes-agent'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_runtime_value('hermes_analysis_model', value)
        self.assertEqual('90', validate_runtime_value('quote_max_age_seconds', '090'))
        self.assertEqual('', validate_runtime_value('hermes_analysis_model', ''))

    async def test_snapshot_changes_next_operation_and_invalid_cipher_fails_closed(self):
        settings=self.settings();conn=SimpleNamespace(fetch=AsyncMock(return_value=[]))
        original=await runtime_configuration(settings,conn)
        self.assertEqual(15,original['quote_max_age_seconds'])
        conn.fetch.return_value=[{'setting_key':'quote_max_age_seconds','encrypted_value':encrypt_secret(b'k'*32,'90')}]
        changed=await runtime_configuration(settings,conn)
        self.assertEqual(90,await runtime_quote_max_age(settings,conn))
        self.assertNotEqual(original['configuration_version'],changed['configuration_version'])
        conn.fetch.return_value[0]['encrypted_value']='invalid'
        with self.assertRaises(HTTPException) as caught:
            await runtime_configuration(settings,conn)
        self.assertEqual(503,caught.exception.status_code)

    async def test_internal_snapshot_provider_model_reaches_hermes_without_upstream_key(self):
        settings=self.settings();version='a'*64;requests=[]
        async def handler(request):
            requests.append(request)
            if request.url.host=='api':
                return httpx.Response(200,json={'model':'cline-pass/deepseek-v4.1-flash',
                    'provider':'custom:cline','configuration_version':version})
            body=json.loads(request.content)
            self.assertEqual('custom:cline',body['provider'])
            self.assertEqual('cline-pass/deepseek-v4.1-flash',body['model'])
            self.assertNotIn('api_key',body)
            return httpx.Response(200,json={'model':body['model'],'choices':[{'finish_reason':'stop',
                'message':{'content':'已完成工具核验'}}]})
        factory=httpx.AsyncClient
        with patch.object(hermes_client.httpx,'AsyncClient',side_effect=lambda **kw:factory(transport=httpx.MockTransport(handler),**kw)):
            snapshot=await hermes_client.resolve_review_settings(settings)
            self.assertEqual(version,snapshot.configuration_version)
            with patch.object(hermes_client,'_content_parts',AsyncMock(return_value=[{'type':'text','text':'sample'}])):
                await hermes_client.review_message(snapshot,{},uuid.uuid4(),uuid.uuid4())
        self.assertEqual(['api','hermes'],[r.url.host for r in requests])
        self.assertEqual('hermes-agent',settings.hermes_model)

    async def test_internal_config_failure_never_falls_back_to_stale_model(self):
        settings=self.settings();factory=httpx.AsyncClient
        def handler(request): return httpx.Response(503,json={'detail':'unavailable'})
        with patch.object(hermes_client.httpx,'AsyncClient',side_effect=lambda **kw:factory(transport=httpx.MockTransport(handler),**kw)):
            with self.assertRaises(httpx.HTTPStatusError):
                await hermes_client.resolve_review_settings(settings)
