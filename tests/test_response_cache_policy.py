"""Real middleware preserves private evidence without caching API errors."""
import unittest
import asyncio
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx

from app.main import request_context


class ResponseCachePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def response(self, path, *, policy=None, status=200):
        app = FastAPI()
        app.middleware('http')(request_context)

        @app.get(path)
        async def read():
            headers = {'Cache-Control': policy} if policy else {}
            return JSONResponse({'ok': True}, status_code=status, headers=headers)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            return await client.get(path)

    async def test_evidence_private_rule_and_request_headers_survive_middleware(self):
        response = await self.response('/api/v1/public/messages/test', policy='private, no-store')
        self.assertEqual('private, no-store', response.headers['cache-control'])
        self.assertIn('x-request-id', response.headers)
        self.assertEqual('nosniff', response.headers['x-content-type-options'])

    async def test_all_api_success_and_error_responses_are_private_without_trusting_route_public(self):
        for status in (200, 401, 403, 404, 409, 503):
            with self.subTest(status=status):
                response = await self.response('/api/v1/public/messages/test', policy='public, max-age=3600', status=status)
                self.assertEqual(status, response.status_code)
                self.assertEqual('private, no-store', response.headers['cache-control'])

    async def test_internal_responses_keep_no_store(self):
        response = await self.response('/internal/review/config', policy='public, max-age=3600')
        self.assertEqual('no-store', response.headers['cache-control'])

    async def test_telegram_request_timing_logs_only_fixed_operation_metadata(self):
        app = FastAPI()
        app.middleware('http')(request_context)

        @app.post('/internal/telegram/events')
        async def event():
            await asyncio.sleep(0.001)
            return JSONResponse({'ok': True})

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            with patch('app.main.log.info') as info:
                response = await client.post('/internal/telegram/events', json={
                    'chat_id': 'private-chat-id', 'raw_text': 'private recommendation'})
        self.assertEqual(200, response.status_code)
        message = info.call_args.args[0]
        fields = info.call_args.args[1:]
        rendered = message % fields
        self.assertIn('operation=events status=200 category=none elapsed_ms=', rendered)
        self.assertNotIn('private-chat-id', rendered)
        self.assertNotIn('private recommendation', rendered)

    async def test_telegram_event_phase_log_contains_only_numeric_timings(self):
        app = FastAPI()
        app.middleware('http')(request_context)

        @app.post('/internal/telegram/events')
        async def event(request: Request):
            request.state.telegram_event_phases = {
                'pool_wait_ms': 2.5,
                'source_message_lock_ms': 4.0,
                'event_sql_ms': 7.5,
            }
            return JSONResponse({'ok': True})

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            with patch('app.main.log.info') as info:
                response = await client.post('/internal/telegram/events', json={
                    'chat_id': 'private-chat-id', 'raw_text': 'private recommendation'})
        self.assertEqual(200, response.status_code)
        rendered = '\n'.join(call.args[0] % call.args[1:] for call in info.call_args_list)
        self.assertIn('pool_wait_ms=2.5', rendered)
        self.assertIn('source_message_lock_ms=4.0', rendered)
        self.assertIn('event_sql_ms=7.5', rendered)
        self.assertIn('remaining_ms=', rendered)
        self.assertNotIn('private-chat-id', rendered)
        self.assertNotIn('private recommendation', rendered)

    async def test_public_health_default_and_explicit_no_store_are_preserved(self):
        default = await self.response('/healthz')
        self.assertEqual('public, max-age=60', default.headers['cache-control'])
        uncached = await self.response('/uncached', policy='no-store')
        self.assertEqual('no-store', uncached.headers['cache-control'])
        private = await self.response('/private', policy='Private, no-store')
        self.assertEqual('private, no-store', private.headers['cache-control'])
        error = await self.response('/failed', status=503)
        self.assertEqual('no-store', error.headers['cache-control'])
