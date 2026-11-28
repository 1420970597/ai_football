"""A model cannot declare an already-read signed handicap option absent."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest
import uuid

from app import worker
from app.review_completion import completion_issues, continuation_prompt
from app.review_summary import record_summary
from app.review_state import trace
import test_reviews as support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated PostgreSQL')
class ReadQuoteClaimCompletionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown
    context = support.ReviewTests.context
    post = support.ReviewTests.post
    fixture = support.ReviewTests.fixture
    search_all = support.ReviewTests.search_all
    resolve = support.ReviewTests.resolve

    async def prepare(self, *, selection='home', line='1.5', period='full_time'):
        self.fixture_id = await self.fixture(1, id='leyu:77001', markets=[dict(
            market_id='39', market_name='全场让分', kind='handicap', period=period,
            line=line, selection=selection, price='1.95', status='active',
            source_updated_at=self.now.isoformat(), received_at=self.now.isoformat())])
        key, _ = await self.search_all()
        await self.resolve(key)
        response = await self.post('list_match_markets', fixture_id=self.fixture_id)
        self.assertEqual(200, response.status_code, response.text)
        self.quote_id = uuid.UUID(response.json()['markets'][0]['quote_id'])

    def denial(self, number='+1.5', *, prefix='Alpha1'):
        return f'{prefix}：该场让球盘库存只有1条，没有 {number}，因此无法录入。'

    async def issues(self, text):
        async with self.pool.acquire() as conn:
            return await completion_issues(conn, self.job, self.attempt, set(), final_content=text)

    async def test_native_absence_claim_contradicts_this_attempt_read_quote(self):
        await self.prepare()
        issues = await self.issues(self.denial())
        self.assertEqual(1, issues.get('read_quote_claim_conflict_count'))
        conflict = issues['read_quote_claim_conflicts'][0]
        self.assertEqual('leyu:77001', conflict['fixture_id'])
        self.assertEqual(str(self.quote_id), conflict['quote_id'])
        self.assertIn('已读', continuation_prompt(issues))
        for table in ('v2_expert_signals', 'v2_bet_orders'):
            self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM '+table))

    async def test_worker_retries_false_missing_quote_instead_of_approving_completion(self):
        await self.prepare()
        model = AsyncMock(return_value=(self.denial(), {'content': self.denial()}))
        with patch.object(worker, 'review_message', model):
            _, summary = await worker._review_until_verified(self.pool, SimpleNamespace(),
                dict(id=self.job, attempt_id=self.attempt, message={}))
        self.assertFalse(summary['completion_verified'])
        self.assertEqual(3, model.await_count)
        self.assertEqual(1, summary['completion_issues']['read_quote_claim_conflict_count'])

    async def test_final_receipt_independently_rechecks_model_completion_flag(self):
        await self.prepare()
        async with self.pool.acquire() as conn, conn.transaction():
            await trace(conn, self.job, 'hermes_result', dict(attempt_id=str(self.attempt),
                content=self.denial(), completion_verified=True))
            await record_summary(conn, dict(id=self.job, message_id=self.message), self.attempt, 'real_time')
        receipt = await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE trace_kind='system_review_result'")
        self.assertFalse(receipt['completion_verified'])
        self.assertEqual(1, receipt['completion_issues']['read_quote_claim_conflict_count'])

    async def test_correction_can_explain_identity_uncertainty_without_recording(self):
        await self.prepare()
        corrected = 'Alpha1已读盘口包含受让1.5，但原图对手身份仍无法确认，保持未解决。'
        model = AsyncMock(side_effect=[(self.denial(), {}), (corrected, {})])
        with patch.object(worker, 'review_message', model):
            _, summary = await worker._review_until_verified(self.pool, SimpleNamespace(),
                dict(id=self.job, attempt_id=self.attempt, message={}))
        self.assertTrue(summary['completion_verified'])
        self.assertEqual(2, model.await_count)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_unread_foreign_attempt_and_closed_options_are_not_hints(self):
        await self.prepare()
        await self.pool.execute('UPDATE v2_review_market_reads SET attempt_id=$2 WHERE job_id=$1', self.job, uuid.uuid4())
        self.assertEqual({}, await self.issues(self.denial()))
        await self.pool.execute('UPDATE v2_review_market_reads SET attempt_id=$2 WHERE job_id=$1', self.job, self.attempt)
        await self.pool.execute("UPDATE v2_current_quotes SET status='closed' WHERE id=$1", self.quote_id)
        self.assertEqual({}, await self.issues(self.denial()))

    async def test_away_selected_sign_uses_provider_home_line(self):
        await self.prepare(selection='away', line='-1.5')
        self.assertEqual(1, (await self.issues(self.denial(prefix='Beta1'))).get('read_quote_claim_conflict_count'))
        self.assertEqual({}, await self.issues(self.denial('-1.5', prefix='Beta1')))

    async def test_other_period_other_line_and_unattributed_team_are_not_hints(self):
        await self.prepare(period='first_half')
        for text in (self.denial(prefix='Alpha1全场'), self.denial('+2.5'), self.denial(prefix='Other Team')):
            with self.subTest(text=text):
                self.assertEqual({}, await self.issues(text))

    async def test_quoted_and_historical_denials_are_not_current_claims(self):
        await self.prepare()
        for text in ('> '+self.denial(), '原文：'+self.denial(), '```\n'+self.denial()+'\n```',
                     '不能断言'+self.denial(), '引用“'+self.denial()+'”'):
            with self.subTest(text=text):
                self.assertEqual({}, await self.issues(text))

    async def test_shared_team_name_does_not_attribute_claim_to_one_match(self):
        await self.prepare()
        await self.fixture(2, id='leyu:77002', home_team='Alpha1', markets=[])
        key, _ = await self.search_all('Beta2')
        bound = await self.post('resolve_match', search_key=key, team_a='Alpha1', team_b='Beta2')
        self.assertTrue(bound.json()['resolved'])
        self.assertEqual({}, await self.issues(self.denial()))
        explicit = self.denial(prefix='leyu:77001 Alpha1')
        self.assertEqual(1, (await self.issues(explicit)).get('read_quote_claim_conflict_count'))

    async def test_other_selected_team_and_changed_read_context_do_not_supply_evidence(self):
        await self.prepare(selection='away', line='-1.5')
        self.assertEqual({}, await self.issues(self.denial(prefix='Alpha1')))
        await self.pool.execute("UPDATE v2_current_quotes SET selection='home',line=1.5 WHERE id=$1", self.quote_id)
        self.assertEqual({}, await self.issues(self.denial()))

    async def test_no_period_claim_does_not_choose_between_two_read_periods(self):
        await self.prepare()
        await self.pool.execute(
            "INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,market_kind,period,line,selection,price,status,received_at,source_updated_at) "
            "SELECT $2,fixture_id,market_id,market_name,market_kind,'first_half',line,selection,price,status,received_at,source_updated_at "
            "FROM v2_current_quotes WHERE id=$1", self.quote_id, uuid.uuid4())
        read = await self.post('list_match_markets', fixture_id=self.fixture_id)
        self.assertEqual(200, read.status_code, read.text)
        self.assertEqual({}, await self.issues(self.denial()))
        self.assertEqual(1, (await self.issues(self.denial(prefix='Alpha1全场'))).get('read_quote_claim_conflict_count'))
