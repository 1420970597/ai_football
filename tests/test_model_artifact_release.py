"""Software artifact handoff contracts with an isolated PostgreSQL database."""
from copy import deepcopy
import asyncio
from unittest.mock import patch
import unittest

from fastapi import HTTPException
import httpx

from app import hermes_client, model_artifact_release as releases, model_configuration as models
from app.routes import internal
from app.runtime_settings import runtime_configuration
import test_identity as support


@unittest.skipUnless(support.TEST_URL, 'requires isolated database')
class PromptArtifactReleaseTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = support.IdentityIntegrationTests.asyncTearDown

    async def asyncSetUp(self):
        await support.IdentityIntegrationTests.asyncSetUp(self)
        self.app.include_router(internal.router)
        self.settings = self.app.state.settings
        self.settings.hermes_api_key = 'isolated-prompt-release-token'
        self.settings.hermes_provider = 'custom:cline'
        self.settings.hermes_model = 'hermes-agent'
        self.settings.review_config_url = 'http://testserver/internal/review/config'
        self.old_prompt = hermes_client.SYSTEM_PROMPT + '\nold isolated release'
        with patch.object(hermes_client, 'SYSTEM_PROMPT', self.old_prompt):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app)) as client:
                await hermes_client.resolve_review_settings(self.settings, client=client)
            self.before = deepcopy(await models.read_state(self.pool, self.settings))
            self.old_snapshot = await runtime_configuration(self.settings, self.pool)
            self.old_token = models.receipt_token(self.settings, self.old_snapshot)
        self.arguments = dict(release_id='isolated-prompt-release', source_commit='a' * 40,
            test_report_sha256='b' * 64, test_count=1603,
            expected_active_version=self.before['active']['version'],
            expected_prompt_sha256=self.before['active']['provenance']['prompt_sha256'])

    async def deploy(self, **changes):
        async with self.pool.acquire() as conn, conn.transaction():
            return await releases.deploy_prompt_artifact(conn, self.settings, **{**self.arguments, **changes})

    async def state(self):
        return await models.read_state(self.pool, self.settings)

    async def test_new_worker_consumes_and_old_inflight_ack_preserves_new_active_receipt(self):
        result = await self.deploy()
        self.assertTrue(result['changed'])
        state = await self.state()
        self.assertEqual(self.before['last_consumption'], state['last_consumption'])
        self.assertEqual(self.before['consumed_versions'], state['consumed_versions'])
        self.assertEqual(self.before['active'], state['artifact_deployments'][0]['before'])
        self.assertEqual(state['active'], state['artifact_deployments'][0]['after'])
        self.assertFalse((await runtime_configuration(self.settings, self.pool))['model_configuration']['consumption_acknowledged'])
        for key in ('model', 'provider', 'artifact_verification'):
            self.assertEqual(self.before['active'][key], state['active'][key])
        for key in models.PROVENANCE:
            if key != 'prompt_sha256':
                self.assertEqual(self.before['active']['provenance'][key], state['active']['provenance'][key])
        self.assertEqual(models.prompt_digest(), state['active']['provenance']['prompt_sha256'])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app)) as client:
            frozen = await hermes_client.resolve_review_settings(self.settings, client=client)
            self.assertEqual(result['active_version'], frozen.model_configuration_version)
            current = deepcopy((await self.state())['last_consumption'])
            response = await client.post('http://testserver/internal/review/config/ack',
                headers={'authorization': 'Bearer ' + self.settings.hermes_api_key}, json={
                    'token': self.old_token, 'model': self.old_snapshot['model'],
                    'provider': self.old_snapshot['model_configuration']['provider'],
                    'prompt_sha256': self.arguments['expected_prompt_sha256']})
            self.assertEqual(200, response.status_code)
            self.assertFalse(response.json()['current'])
        self.assertEqual(current, (await self.state())['last_consumption'])
        self.assertEqual(2, await self.pool.fetchval("SELECT count(*) FROM v2_audit_log WHERE action='model_configuration_consumed'"))
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_audit_log WHERE action='model_prompt_artifact_deployed'"))
        with patch.object(hermes_client, 'SYSTEM_PROMPT', self.old_prompt):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app)) as client:
                with self.assertRaisesRegex(RuntimeError, 'provenance'):
                    await hermes_client.resolve_review_settings(self.settings, client=client)

    async def test_same_release_is_idempotent_and_conflicting_reuse_is_rejected(self):
        first = await self.deploy()
        state = await self.state()
        self.assertFalse((await self.deploy())['changed'])
        self.assertEqual(first['active_version'], (await self.state())['active']['version'])
        self.assertEqual(state, await self.state())
        with self.assertRaises(HTTPException) as caught:
            await self.deploy(test_report_sha256='c' * 64)
        self.assertEqual(409, caught.exception.status_code)
        self.assertEqual(state, await self.state())

    async def test_concurrent_release_serializes_to_one_revision_and_one_audit(self):
        results = await asyncio.gather(self.deploy(), self.deploy())
        self.assertEqual([False, True], sorted(item['changed'] for item in results))
        self.assertEqual(1, len((await self.state())['artifact_deployments']))
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_audit_log WHERE action='model_prompt_artifact_deployed'"))

    async def test_stale_baseline_and_invalid_evidence_never_mutate_state(self):
        cases = [({'expected_active_version': 'f' * 64}, 409),
                 ({'expected_prompt_sha256': 'f' * 64}, 409),
                 ({'source_commit': 'not-a-commit'}, 422),
                 ({'test_report_sha256': 'not-a-hash'}, 422),
                 ({'test_count': 0}, 422), ({'test_count': True}, 422)]
        for arguments, status in cases:
            with self.subTest(arguments=arguments), self.assertRaises(HTTPException) as caught:
                await self.deploy(**arguments)
            self.assertEqual(status, caught.exception.status_code)
            self.assertEqual(self.before, await self.state())

    async def test_release_and_audit_roll_back_together(self):
        with self.assertRaisesRegex(RuntimeError, 'isolated rollback'):
            async with self.pool.acquire() as conn, conn.transaction():
                await releases.deploy_prompt_artifact(conn, self.settings, **self.arguments)
                raise RuntimeError('isolated rollback')
        self.assertEqual(self.before, await self.state())
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_audit_log WHERE action='model_prompt_artifact_deployed'"))

    async def test_unchanged_prompt_and_history_capacity_are_closed(self):
        with patch.object(hermes_client, 'SYSTEM_PROMPT', self.old_prompt), self.assertRaises(HTTPException) as caught:
            await self.deploy()
        self.assertEqual(409, caught.exception.status_code)
        await self.deploy()
        current = await self.state()
        with patch.object(releases, 'MAX_RELEASES', 1), patch.object(hermes_client, 'SYSTEM_PROMPT', 'second isolated release'):
            with self.assertRaises(HTTPException) as caught:
                await self.deploy(release_id='second-release',
                    expected_active_version=current['active']['version'],
                    expected_prompt_sha256=current['active']['provenance']['prompt_sha256'])
        self.assertEqual(413, caught.exception.status_code)
        self.assertEqual(current, await self.state())

    async def test_tampered_history_cannot_authorize_old_versions_or_change_other_artifacts(self):
        await self.deploy()
        state = await self.state()
        for change in ('digest', 'model', 'framework', 'duplicate'):
            altered = deepcopy(state)
            item = altered['artifact_deployments'][0]
            if change == 'duplicate':
                altered['artifact_deployments'].append(deepcopy(item))
            elif change == 'digest':
                item['before']['version'] = '0' * 64
            else:
                if change == 'model':
                    item['after']['model'] = 'different-model'
                else:
                    item['after']['provenance']['hermes_version'] = 'different-framework'
                item['after']['version'] = models.digest({k: v for k, v in item['after'].items() if k != 'version'})
            await models.save_state(self.pool, self.settings, altered)
            with self.subTest(change=change), self.assertRaises(HTTPException) as caught:
                await self.state()
            self.assertEqual(503, caught.exception.status_code)
        await models.save_state(self.pool, self.settings, state)
        self.assertEqual(state, await self.state())

    async def test_pending_model_candidates_survive_and_cannot_activate_against_old_baseline(self):
        async with self.pool.acquire() as conn, conn.transaction():
            candidate, _ = await models.stage(conn, self.settings, None, 'isolated-candidate',
                provenance={**self.before['active']['provenance'], 'model_id': 'isolated-candidate'})
        pending = deepcopy((await self.state())['candidates'])
        await self.deploy()
        state = await self.state()
        self.assertEqual(pending, state['candidates'])
        self.assertNotEqual(state['active']['version'], candidate['baseline_version'])
