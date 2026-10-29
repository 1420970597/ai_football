"""Governance contract fixtures, not evidence of a real labelled model improvement."""
import asyncio
from copy import deepcopy
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

import httpx

from app import auth, hermes_client, model_configuration as models
from app.routes import admin, internal
from app.runtime_settings import runtime_configuration, runtime_quote_max_age
from app.security import encrypt_secret, decrypt_secret, token_hash
import test_identity as support


@unittest.skipUnless(support.TEST_URL, 'requires isolated database')
class ModelConfigurationTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = support.IdentityIntegrationTests.asyncTearDown
    admin_login = support.IdentityIntegrationTests.admin_login
    csrf = support.IdentityIntegrationTests.csrf
    register = support.IdentityIntegrationTests.register

    async def asyncSetUp(self):
        await support.IdentityIntegrationTests.asyncSetUp(self)
        self.app.include_router(internal.router)
        settings = self.app.state.settings
        settings.hermes_api_key = 'isolated-internal-model-worker'
        settings.hermes_provider = 'custom:cline'
        settings.hermes_model = 'hermes-agent'
        settings.quote_max_age_seconds = 15
        settings.review_config_url = 'http://testserver/internal/review/config'
        await self.pool.execute("INSERT INTO v2_admin_settings(setting_key,encrypted_value) VALUES('hermes_analysis_model',$1)",
            encrypt_secret(settings.encryption_key, 'cline-pass/deepseek-v4.1-flash'))
        await self.admin_login()

    async def status(self):
        response = await self.client.get('/api/v1/admin/model-configuration')
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    async def create(self, *, model='candidate/model-v2', provenance=None, revision=None):
        state = await self.status()
        provenance = provenance or {**state['active']['provenance'], 'model_id': model, 'model_revision': 'frozen-model-2'}
        response = await self.client.post('/api/v1/admin/model-configuration/candidates', headers=self.csrf(True), json={
            'model': model, 'provenance': provenance, 'expected_revision': revision or state['revision']})
        self.assertEqual(201, response.status_code, response.text)
        return response.json()['candidate']

    async def call(self, candidate, operation, **body):
        if 'expected_revision' not in body:
            body['expected_revision'] = (await self.status())['revision']
        return await self.client.post('/api/v1/admin/model-configuration/candidates/'+candidate['id']+'/'+operation,
            headers=self.csrf(True), json=body)

    async def report(self, candidate, *, synthetic=False):
        state = await self.status()
        return {'format': 'football-review-comparison-v1' if synthetic else 'football-review-comparison-real-v1',
            'synthetic_only': synthetic, 'automatic_promotion': False, 'dataset_id': 'explicit-test-fixture',
            'annotation_version': 'test-human-contract-v1', 'label_digest_sha256': 'a'*64,
            'baseline_provenance': state['active']['provenance'], 'candidate_provenance': candidate['provenance'],
            'provenance_missing': {'baseline': [], 'candidate': []},
            'baseline_metrics': {'coverage': {'samples': 1, 'assessed': 1}},
            'candidate_metrics': {'coverage': {'samples': 1, 'assessed': 1}},
            'metric_changes': {'exact_binding': {'correct': 1}}, 'changed_samples': 1,
            'review_queue': [{'sample_id': 'fixture-one', 'reasons': ['accepted_bindings_changed'],
                'baseline': {'accepted': []}, 'candidate': {'accepted': []}}],
            'source_evidence': None if synthetic else {'kind': 'human_annotated', 'labels_reviewed': True,
                'source_manifest_sha256': 'b'*64, 'sample_count': 1, 'annotation_version': 'test-human-contract-v1'}}

    async def prepare(self, candidate):
        report = await self.report(candidate)
        response = await self.call(candidate, 'evidence', report=report, report_sha256=models.digest(report))
        self.assertEqual(200, response.status_code, response.text)
        response = await self.call(candidate, 'review', report_sha256=models.digest(report), candidate_fingerprint=candidate['fingerprint'],
            reason='Explicit isolated contract review', decisions=[{'sample_id': 'fixture-one', 'decision': 'accept', 'note': 'Reviewed test contract'}])
        self.assertEqual(200, response.status_code, response.text)

    async def test_legacy_baseline_remains_active_pending_save_and_ttl_is_immediate(self):
        original = await self.status()
        response = await self.client.put('/api/v1/admin/settings', headers=self.csrf(True), json={
            'expected_model_revision': original['revision'], 'values': {'hermes_analysis_model': 'candidate/model-v2', 'quote_max_age_seconds': '90'}})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('model_candidate_pending', response.json()['application']['state'])
        state = await self.status()
        self.assertEqual(original['active'], state['active'])
        self.assertEqual('pending_evidence', state['candidates'][0]['status'])
        self.assertEqual(90, await runtime_quote_max_age(self.app.state.settings))
        self.assertEqual('cline-pass/deepseek-v4.1-flash', (await runtime_configuration(self.app.state.settings))['model'])
        self.assertEqual('cline-pass/deepseek-v4.1-flash', decrypt_secret(self.app.state.settings.encryption_key,
            await self.pool.fetchval("SELECT encrypted_value FROM v2_admin_settings WHERE setting_key='hermes_analysis_model'")))
        with patch.object(admin, 'collector_health', AsyncMock(return_value={'state': 'unavailable'})):
            output = (await self.client.get('/api/v1/admin/settings')).json()
        self.assertNotIn(models.STATE_KEY, output['items'])
        self.assertEqual('cline-pass/deepseek-v4.1-flash', output['items']['hermes_analysis_model']['value'])

    async def test_missing_mismatched_synthetic_or_unreviewed_evidence_cannot_activate(self):
        candidate = await self.create()
        self.assertEqual(409, (await self.call(candidate, 'activate')).status_code)
        report = await self.report(candidate)
        self.assertEqual(409, (await self.call(candidate, 'evidence', report=report, report_sha256='c'*64)).status_code)
        bad = deepcopy(report); bad['candidate_provenance']['skill_version'] = 'wrong-skill'
        self.assertEqual(409, (await self.call(candidate, 'evidence', report=bad, report_sha256=models.digest(bad))).status_code)
        synthetic = await self.report(candidate, synthetic=True)
        self.assertEqual(200, (await self.call(candidate, 'evidence', report=synthetic, report_sha256=models.digest(synthetic))).status_code)
        self.assertEqual('synthetic_diagnostic_only', (await self.status())['candidates'][0]['status'])
        self.assertEqual(409, (await self.call(candidate, 'review', report_sha256=models.digest(synthetic), candidate_fingerprint=candidate['fingerprint'], reason='Fixture review', decisions=[])).status_code)
        self.assertEqual(409, (await self.call(candidate, 'activate')).status_code)

    async def test_review_requires_all_differences_and_activation_is_atomic_versioned(self):
        candidate = await self.create()
        report = await self.report(candidate)
        self.assertEqual(200, (await self.call(candidate, 'evidence', report=report, report_sha256=models.digest(report))).status_code)
        self.assertEqual(422, (await self.call(candidate, 'review', report_sha256=models.digest(report), candidate_fingerprint=candidate['fingerprint'], reason='Review all fixture differences', decisions=[])).status_code)
        response = await self.call(candidate, 'review', report_sha256=models.digest(report), candidate_fingerprint=candidate['fingerprint'],
            reason='Explicit isolated human review contract', decisions=[{'sample_id': 'fixture-one', 'decision': 'accept', 'note': 'Checked frozen difference'}])
        self.assertEqual(200, response.status_code, response.text)
        old = await self.status()
        responses = await asyncio.gather(*[self.call(candidate, 'activate', expected_revision=old['revision']) for _ in range(2)])
        self.assertEqual([200,409], sorted(response.status_code for response in responses))
        state = await self.status()
        self.assertEqual('candidate/model-v2', state['active']['model'])
        self.assertEqual(old['revision']+1, state['revision'])
        self.assertEqual('awaiting_consumer', state['application_state'])
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_audit_log WHERE action='model_candidate_activate'"))

    async def test_candidate_drift_duplicate_stale_revision_and_artifact_change_rejected(self):
        candidate = await self.create()
        self.assertEqual(409, (await self.client.post('/api/v1/admin/model-configuration/candidates', headers=self.csrf(True), json={
            'model': candidate['model'], 'provenance': candidate['provenance'], 'expected_revision': (await self.status())['revision']})).status_code)
        changed = {**(await self.status())['active']['provenance'], 'model_id': 'candidate/skill-change',
            'model_revision': 'frozen-skill-candidate', 'skill_version': 'new-skill-unverified'}
        other = await self.create(model='candidate/skill-change', provenance=changed)
        await self.prepare(other)
        self.assertEqual(409, (await self.call(other, 'activate')).status_code)
        self.assertEqual(409, (await self.call(candidate, 'activate', expected_revision=1)).status_code)
        state = await models.read_state(self.pool, self.app.state.settings)
        state['candidates'][0]['provenance']['model_revision'] = 'changed-after-freeze'
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await models.save_state(conn, self.app.state.settings, state, self.admin_id)
        self.assertEqual(409, (await self.call(candidate, 'activate')).status_code)

    async def test_wrong_role_csrf_and_session_revoked_while_waiting_do_not_mutate(self):
        state = await self.status()
        body = {'model': 'candidate/test', 'provenance': {**state['active']['provenance'], 'model_id': 'candidate/test'}, 'expected_revision': 1}
        self.assertEqual(403, (await self.client.post('/api/v1/admin/model-configuration/candidates', json=body)).status_code)
        original = admin.require_admin
        async def revoked(request):
            identity = await original(request)
            await self.pool.execute('DELETE FROM v2_sessions WHERE token_hash=$1', identity['_token_hash'])
            return identity
        with patch.object(admin, 'require_admin', side_effect=revoked):
            self.assertEqual(401, (await self.client.post('/api/v1/admin/model-configuration/candidates', headers=self.csrf(True), json=body)).status_code)
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_audit_log WHERE action='model_candidate_created'"))
        await self.register()
        self.assertIn((await self.client.get('/api/v1/admin/model-configuration')).status_code, (401,403))

    async def test_internal_snapshot_ack_and_immutable_worker_consumption(self):
        candidate = await self.create(); await self.prepare(candidate)
        self.assertEqual(200, (await self.call(candidate, 'activate')).status_code)
        factory = httpx.AsyncClient
        with patch.object(hermes_client.httpx, 'AsyncClient', side_effect=lambda **kw: factory(transport=httpx.ASGITransport(app=self.app), **kw)):
            snapshot = await hermes_client.resolve_review_settings(self.app.state.settings)
        self.assertEqual('candidate/model-v2', snapshot.hermes_model)
        self.assertEqual('consumed', (await self.status())['application_state'])
        with self.assertRaises(AttributeError):
            snapshot.hermes_model = 'changed'
        with patch.object(hermes_client.httpx, 'AsyncClient', side_effect=AssertionError('same snapshot must not reread')):
            self.assertIs(snapshot, await hermes_client.resolve_review_settings(snapshot))
        config = await self.client.get('/internal/review/config', headers={'Authorization': 'Bearer isolated-internal-model-worker'})
        self.assertEqual(200, config.status_code)
        value = config.json()
        self.assertEqual(401, (await self.client.post('/internal/review/config/ack', json={'token': value['consumption_token'],
            'model': value['model'], 'provider': value['provider'], 'prompt_sha256': models.prompt_digest()})).status_code)
        bad = await self.client.post('/internal/review/config/ack', headers={'Authorization': 'Bearer isolated-internal-model-worker'},
            json={'token': value['consumption_token'], 'model': 'wrong/model', 'provider': value['provider'], 'prompt_sha256': models.prompt_digest()})
        self.assertEqual(409, bad.status_code)

    async def test_deployed_persisted_legacy_baseline_remains_consumable(self):
        # This is the artifact fingerprint already acknowledged by the deployed
        # worker, rather than a baseline generated from the code under test.
        fingerprint = '222388e0dde4957ff096435cdf283f676e1c01e8003c9f536599f76f30a84173'
        state = models.legacy_state(self.app.state.settings, 'cline-pass/deepseek-v4.1-flash')
        state['active']['provenance']['prompt_sha256'] = fingerprint
        state['active']['version'] = models.digest({k: v for k, v in state['active'].items() if k != 'version'})
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await models.save_state(conn, self.app.state.settings, state, self.admin_id)
        factory = httpx.AsyncClient
        with patch.object(hermes_client.httpx, 'AsyncClient', side_effect=lambda **kw: factory(transport=httpx.ASGITransport(app=self.app), **kw)):
            snapshot = await hermes_client.resolve_review_settings(self.app.state.settings)
        self.assertEqual('cline-pass/deepseek-v4.1-flash', snapshot.hermes_model)
        consumed = await models.read_state(self.pool, self.app.state.settings)
        self.assertEqual(state['active'], consumed['active'])
        self.assertEqual(state['revision'], consumed['revision'])
        self.assertEqual(fingerprint, consumed['active']['provenance']['prompt_sha256'])
        self.assertEqual(state['active']['version'], snapshot.model_configuration_version)

    async def test_model_governance_cipher_failure_does_not_disable_quote_ttl(self):
        await self.create()
        await self.pool.execute('UPDATE v2_admin_settings SET encrypted_value=$1 WHERE setting_key=$2', 'invalid', models.STATE_KEY)
        self.assertEqual(15, await runtime_quote_max_age(self.app.state.settings))
        with self.assertRaises(Exception) as caught:
            await runtime_configuration(self.app.state.settings)
        self.assertEqual(503, caught.exception.status_code)

    async def test_malformed_report_fields_and_invalid_model_rejected_without_state_change(self):
        state = await self.status()
        invalid = await self.client.post('/api/v1/admin/model-configuration/candidates', headers=self.csrf(True), json={
            'model': 'https://provider.invalid', 'provenance': state['active']['provenance'], 'expected_revision': state['revision']})
        self.assertEqual(422, invalid.status_code)
        self.assertEqual(state, await self.status())
        candidate = await self.create()
        original = await self.status()
        report = await self.report(candidate)
        variants = []
        value = deepcopy(report); value['candidate_provenance'] = []
        variants.append(value)
        value = deepcopy(report); value['baseline_metrics'] = []
        variants.append(value)
        value = deepcopy(report); value['candidate_metrics']['coverage']['samples'] = True
        variants.append(value)
        value = deepcopy(report); value['synthetic_only'] = 0
        variants.append(value)
        value = deepcopy(report); value['source_evidence']['headers'] = {'Authorization': 'test-only-fixture'}
        variants.append(value)
        for malformed in variants:
            with self.subTest(kind=variants.index(malformed)):
                response = await self.call(candidate, 'evidence', report=malformed, report_sha256=models.digest(malformed))
                self.assertIn(response.status_code, (409, 422), response.text)
                self.assertEqual(original, await self.status())

    async def test_old_inflight_snapshot_ack_never_marks_new_active_consumed(self):
        headers = {'Authorization': 'Bearer isolated-internal-model-worker'}
        old = (await self.client.get('/internal/review/config', headers=headers)).json()
        candidate = await self.create(); await self.prepare(candidate)
        self.assertEqual(200, (await self.call(candidate, 'activate')).status_code)
        response = await self.client.post('/internal/review/config/ack', headers=headers,
            json={'token': old['consumption_token'], 'model': old['model'], 'provider': old['provider'],
                'prompt_sha256': old['model_configuration']['provenance']['prompt_sha256']})
        self.assertEqual(200, response.status_code, response.text)
        self.assertIs(response.json()['current'], False)
        self.assertEqual('awaiting_consumer', (await self.status())['application_state'])
        self.assertEqual('cline-pass/deepseek-v4.1-flash', old['model'])
        response = await self.client.post('/internal/review/config/ack', headers=headers,
            json={'token': old['consumption_token'], 'model': old['model'], 'provider': old['provider'],
                'prompt_sha256': old['model_configuration']['provenance']['prompt_sha256']})
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_audit_log WHERE action='model_configuration_consumed'"))

    async def test_frozen_report_difference_drift_is_rejected(self):
        candidate = await self.create()
        report = await self.report(candidate)
        self.assertEqual(200, (await self.call(candidate, 'evidence', report=report, report_sha256=models.digest(report))).status_code)
        state = await models.read_state(self.pool, self.app.state.settings)
        state['candidates'][0]['report']['differences'][0]['reasons'] = ['changed-after-freeze']
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await models.save_state(conn, self.app.state.settings, state, self.admin_id)
        response = await self.call(candidate, 'review', report_sha256=models.digest(report), candidate_fingerprint=candidate['fingerprint'],
            reason='Explicit test fixture', decisions=[{'sample_id':'fixture-one','decision':'accept','note':'Test fixture only'}])
        self.assertEqual(409, response.status_code, response.text)
