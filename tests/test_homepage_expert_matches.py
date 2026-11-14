"""Read-only homepage projections over isolated fixture evidence."""
import os
import unittest
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from app.routes import public

import test_result_ledger as support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires task-owned isolated database')
class HomepageExpertMatchesTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ResultLedgerTests.asyncSetUp
    asyncTearDown = support.ResultLedgerTests.asyncTearDown
    signal = support.ResultLedgerTests.signal

    async def trusted(self):
        await self.pool.execute('INSERT INTO v2_source_experts(source_id,expert_id) VALUES($1,$2)', self.source, self.expert)

    async def listing(self, **params):
        response = await self.client.get('/api/v1/public/matches', params={
            'recommended_only': 'true', 'include_markets': 'false', **params})
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    async def test_only_recommended_fixtures_with_distinct_market_evidence(self):
        await self.trusted()
        first = await self.signal(line='2.25')
        second = await self.signal(selection='under', line='3.5', period='first_half')
        await self.pool.execute("INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase) "
                                "VALUES('unrelated','unrelated','football','Other','Other home','Other away',$1,'live')", self.now)
        item = (await self.listing(sport='football'))['items'][0]
        self.assertEqual('fixture', item['id'])
        self.assertEqual({str(first), str(second)}, {s['id'] for s in item['expert_signals']})
        self.assertEqual({('full_time', Decimal('2.25'), 'over'), ('first_half', Decimal('3.5'), 'under')},
                         {(s['period'], Decimal(s['selection_line']), s['selection']) for s in item['expert_signals']})
        for record in item['expert_signals']:
            self.assertNotIn('raw_text', record)
            self.assertNotIn('media_objects', record)
        self.assertEqual(1, len((await self.listing())['items']))
        all_matches = (await self.client.get('/api/v1/public/matches')).json()
        self.assertEqual(2, len(all_matches['items']))
        self.assertNotIn('expert_signals', all_matches['items'][0])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_disabled_withdrawn_deleted_and_superseded_records_are_excluded(self):
        await self.trusted()
        signal = await self.signal()
        for table, condition, key in (
            ('v2_expert_sources', 'enabled=false', self.source),
            ('v2_experts', 'enabled=false', self.expert),
            ('v2_source_experts', 'enabled=false', self.source),
            ('v2_expert_signals', 'withdrawn_at=now()', signal),
            ('v2_expert_messages', 'deleted_at=now()', self.message),
        ):
            with self.subTest(table=table):
                column = 'source_id' if table == 'v2_source_experts' else 'id'
                await self.pool.execute(f'UPDATE {table} SET {condition} WHERE {column}=$1', key)
                self.assertEqual([], (await self.listing())['items'])
                restore = 'enabled=true' if 'enabled' in condition else condition.split('=')[0] + '=NULL'
                await self.pool.execute(f'UPDATE {table} SET {restore} WHERE {column}=$1', key)
        await self.pool.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at,revision) "
                                "VALUES($1,$2,$3,'1','updated',now(),2)", uuid.uuid4(), self.source, self.expert)
        self.assertEqual([], (await self.listing())['items'])

    async def test_sport_phase_date_search_and_cursor_scope(self):
        await self.trusted()
        await self.signal()
        await self.pool.execute("INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase) "
                                "VALUES('basketball','basketball','basketball','Basket League','Basket home','Basket away',$1,'scheduled')", self.now+timedelta(minutes=1))
        basketball_signal = await self.signal(line='220.5')
        await self.pool.execute("UPDATE v2_expert_signals SET fixture_id='basketball',market_id='38' WHERE id=$1", basketball_signal)
        self.assertEqual(['fixture'], [m['id'] for m in (await self.listing(sport='football', phase='live'))['items']])
        self.assertEqual(['basketball'], [m['id'] for m in (await self.listing(sport='basketball', phase='scheduled'))['items']])
        self.assertEqual([], (await self.listing(sport='football', phase='finished'))['items'])
        self.assertEqual(['basketball'], [m['id'] for m in (await self.listing(q='Basket'))['items']])
        self.assertEqual([], (await self.listing(date=(self.now-timedelta(days=3)).date().isoformat()))['items'])
        first = await self.listing(page_size=1)
        self.assertTrue(first['next_cursor'])
        second = await self.listing(page_size=1, cursor=first['next_cursor'])
        self.assertEqual(['basketball'], [m['id'] for m in second['items']])
        for changes in ({'recommended_only':'false'}, {'sport':'basketball'}):
            response = await self.client.get('/api/v1/public/matches', params={
                'recommended_only':'true','cursor':first['next_cursor'], **changes})
            self.assertEqual(400, response.status_code, response.text)

    async def test_many_recommendations_are_bounded_and_announced(self):
        await self.trusted()
        for i in range(22):
            await self.signal(line=str(i+1))
        item = (await self.listing())['items'][0]
        self.assertEqual(5, len(item['expert_signals']))
        self.assertTrue(item['expert_signals_has_more'])
        self.assertTrue(item['recommendations_limited'])
        with patch.object(public, '_has_entitlement', AsyncMock(return_value=True)):
            item = (await self.listing())['items'][0]
        self.assertEqual(20, len(item['expert_signals']))
        self.assertTrue(item['expert_signals_has_more'])
        self.assertFalse(item['recommendations_limited'])
