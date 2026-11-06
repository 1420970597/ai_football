import unittest
import asyncio
import hashlib
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app import hermes_client


class HermesClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_cleanup_stops_only_owned_active_attempts(self):
        review, attempt = uuid.uuid4(), uuid.uuid4()
        session = f'expert-review-{review}-{attempt}'
        settings = SimpleNamespace(hermes_api_url='http://hermes.invalid/v1', hermes_api_key='isolated-key')
        calls = []
        deletes = 0
        stopped = False

        async def handler(request):
            nonlocal deletes, stopped
            calls.append((request.method, request.url.path))
            self.assertEqual('Bearer isolated-key', request.headers['Authorization'])
            if request.method == 'DELETE':
                deletes += 1
                return (httpx.Response(409, json={'error': {'code': 'session_active_turn'}})
                        if deletes < 3 else httpx.Response(204))
            if request.method == 'POST':
                self.assertTrue(request.url.path.endswith(str(attempt)+'/stop'))
                stopped = True
                return httpx.Response(200, json={'status': 'stopping'})
            if request.url.path.endswith('-continuation-1'):
                return httpx.Response(200, json={'session_id': session, 'status': 'completed'})
            if request.url.path.endswith('-continuation-2'):
                return httpx.Response(404)
            return httpx.Response(200, json={'session_id': session, 'status': 'cancelled' if stopped else 'running'})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await hermes_client.delete_session(settings, review, attempt, client=client)
        posts = [path for method, path in calls if method == 'POST']
        scope = hashlib.sha256(b'default\0isolated-key').hexdigest()
        self.assertEqual(['/v1/runs/chat:'+scope+':'+str(attempt)+'/stop'], posts)
        self.assertEqual(3, deletes)

    async def test_session_cleanup_does_not_control_other_sessions_or_unknown_states(self):
        for state, same_session in [('running', False), ('unknown', True)]:
            with self.subTest(state=state, same_session=same_session):
                review, attempt = uuid.uuid4(), uuid.uuid4()
                session = f'expert-review-{review}-{attempt}'
                calls = []
                async def handler(request):
                    calls.append(request.method)
                    if request.method == 'DELETE':
                        return httpx.Response(409, json={'error': {'code': 'session_active_turn'}})
                    return httpx.Response(200, json={'session_id': session if same_session else 'another-session',
                                                    'status': state})
                settings = SimpleNamespace(hermes_api_url='http://hermes.invalid/v1', hermes_api_key='isolated-key')
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaises(RuntimeError):
                        await hermes_client.delete_session(settings, review, attempt, client=client)
                self.assertEqual(['DELETE', 'GET'], calls)

    async def test_session_cleanup_normal_and_unrelated_conflicts_do_not_stop_runs(self):
        for status in [200, 204, 404, 403, 409]:
            with self.subTest(status=status):
                calls = []
                async def handler(request):
                    calls.append(request.method)
                    return httpx.Response(status, json={'error': {'code': 'unrelated_conflict'}})
                settings = SimpleNamespace(hermes_api_url='http://hermes.invalid/v1', hermes_api_key='isolated-key')
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    if status in {403, 409}:
                        with self.assertRaises(httpx.HTTPStatusError):
                            await hermes_client.delete_session(settings, uuid.uuid4(), uuid.uuid4(), client=client)
                    else:
                        await hermes_client.delete_session(settings, uuid.uuid4(), uuid.uuid4(), client=client)
                self.assertEqual(['DELETE'], calls)

    async def test_session_cleanup_rejects_run_control_permission_failure(self):
        calls = []
        async def handler(request):
            calls.append(request.method)
            return (httpx.Response(409, json={'error': {'code': 'session_active_turn'}})
                    if request.method == 'DELETE' else httpx.Response(403))
        settings = SimpleNamespace(hermes_api_url='http://hermes.invalid/v1', hermes_api_key='isolated-key')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                await hermes_client.delete_session(settings, uuid.uuid4(), uuid.uuid4(), client=client)
        self.assertEqual(['DELETE', 'GET'], calls)

    async def test_session_cleanup_bounds_a_stalled_run_control(self):
        cancelled = asyncio.Event()
        async def handler(request):
            if request.method == 'DELETE':
                return httpx.Response(409, json={'error': {'code': 'session_active_turn'}})
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        settings = SimpleNamespace(hermes_api_url='http://hermes.invalid/v1', hermes_api_key='isolated-key')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(TimeoutError):
                await hermes_client.delete_session(settings, uuid.uuid4(), uuid.uuid4(), client=client)
        self.assertTrue(cancelled.is_set())

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

    async def test_framework_empty_completion_is_transient_even_with_stop(self):
        template = ("isolated-model didn't produce a reply this time, even after retries. "
                    "Send `continue` to try again, or switch models with /model.")
        contents = ['', ' \n ', template, '⚠️ No reply: '+template, 'No reply: '+template,
                    [{'type': 'text', 'text': '⚠️ No reply: '}, {'type': 'text', 'text': template}],
                    '⚠️ No reply: '+template.replace('isolated-model', 'returned-model'),
                    '⚠️ No reply: '+template.replace('isolated-model', 'The model'),
                    "⚠️ No reply: the model provider didn't answer after all retries. Send /retry, or switch models with /model.",
                    'No reply: the turn stopped while a tool result was still pending. Send `continue` to let the model summarize.',
                    '⚠️ No reply: the turn stopped while a tool result was still pending and the model produced no follow-up text. Send `continue` to let it summarize.']
        settings = SimpleNamespace(hermes_api_url='http://hermes.invalid/v1', hermes_api_key='isolated-key',
                                   hermes_model='isolated-model')
        for content in contents:
            with self.subTest(content=content):
                async def handler(_request):
                    return httpx.Response(200, json={'model': 'returned-model', 'choices': [
                        {'finish_reason': 'stop', 'message': {'content': content}}]})
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with patch.object(hermes_client, '_content_parts', AsyncMock(return_value=[])):
                        with self.assertRaises(hermes_client.HermesProviderUnavailable) as raised:
                            await hermes_client.review_message(settings, {}, uuid.uuid4(), uuid.uuid4(), client=client)
                self.assertEqual('Hermes provider returned a transient error', str(raised.exception))

    async def test_legitimate_review_and_quoted_no_reply_are_not_transient(self):
        template = ("isolated-model didn't produce a reply this time, even after retries. "
                    "Send `continue` to try again, or switch models with /model.")
        settings = SimpleNamespace(hermes_api_url='http://hermes.invalid/v1', hermes_api_key='isolated-key',
                                   hermes_model='isolated-model')
        contents = ['无新增推荐意图', '已接受工具核验结果', '原文引用：⚠️ No reply: '+template,
                    '⚠️ No reply: '+template+'\n补充：已核对推荐。',
                    '⚠️ No reply: '+template.replace('isolated-model', 'different-model')]
        for content in contents:
            with self.subTest(content=content):
                async def handler(_request):
                    return httpx.Response(200, json={'model': 'isolated-model', 'choices': [
                        {'finish_reason': 'stop', 'message': {'content': content}}]})
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with patch.object(hermes_client, '_content_parts', AsyncMock(return_value=[])):
                        result, summary = await hermes_client.review_message(
                            settings, {}, uuid.uuid4(), uuid.uuid4(), client=client)
                self.assertEqual(content, result)
                self.assertEqual(content, summary['content'])


if __name__ == '__main__':
    unittest.main()
