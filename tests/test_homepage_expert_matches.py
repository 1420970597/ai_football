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

    async def signal(self, *args, **kwargs):
        kwargs.setdefault('phase', 'live')
        signal = await support.ResultLedgerTests.signal(self, *args, **kwargs)
        await self.pool.execute(
            "INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,period,market_kind,line,selection,"
            "price,source_updated_at,status,play_id,market_instance_id,selection_id) "
            "SELECT $1,fixture_id,market_id,market_name,period,market_kind,line,selection,2,now(),"
            "'active','native-play','native-market','native-selection' FROM v2_expert_signals WHERE id=$2",
            uuid.uuid4(), signal)
        return signal

    async def move_signal(self, signal, fixture, *, market='total', phase='live'):
        await self.pool.execute(
            "UPDATE v2_current_quotes q SET fixture_id=$2,market_id=$3 FROM v2_expert_signals s "
            "WHERE s.id=$1 AND q.fixture_id=s.fixture_id AND q.line IS NOT DISTINCT FROM s.line "
            "AND q.selection=s.selection AND q.period=s.period", signal, fixture, market)
        await self.pool.execute('UPDATE v2_expert_signals SET fixture_id=$2,market_id=$3,phase=$4 WHERE id=$1',
                                signal, fixture, market, phase)

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

    async def test_cross_date_unsettled_fixture_is_present_without_date_filter(self):
        await self.trusted()
        await self.signal()
        await self.pool.execute("UPDATE v2_fixtures SET kickoff_at=now()-interval '1 day'")
        self.assertEqual(['fixture'], [m['id'] for m in (await self.listing())['items']])
        self.assertEqual([], (await self.listing(date=self.now.date().isoformat()))['items'])

    async def test_kickoff_transition_requires_provably_equivalent_market(self):
        from app.orders import eligible_signal
        from fastapi import HTTPException
        await self.trusted()
        signal = await self.signal(phase='scheduled')
        await self.pool.execute("UPDATE v2_expert_signals SET score_basis='full_period'")
        await self.pool.execute("UPDATE v2_current_quotes SET score_basis='full_period'")
        self.assertEqual(1, len((await self.listing())['items']))
        async with self.pool.acquire() as conn, conn.transaction():
            await eligible_signal(conn, signal, manual=True)
            with self.assertRaises(HTTPException):
                await eligible_signal(conn, signal, manual=False)
        await self.pool.execute("UPDATE v2_current_quotes SET score_basis='remaining_period'")
        self.assertEqual([], (await self.listing())['items'])

    async def test_old_duplicate_messages_do_not_consume_summary_limits(self):
        await self.trusted()
        first = await self.signal()
        for _ in range(7):
            message = uuid.uuid4()
            await self.pool.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) VALUES($1,$2,$3,$4,'repeat',now())",
                                    message,self.source,self.expert,str(message))
            await self.pool.execute("INSERT INTO v2_expert_signals(id,message_id,expert_id,fixture_id,phase,market_id,market_name,market_kind,period,line,selection,recommendation_price,quote_time,confidence,score_basis,score_baseline) "
                "SELECT $1,$2,expert_id,fixture_id,phase,market_id,market_name,market_kind,period,line,selection,recommendation_price,quote_time,confidence,score_basis,score_baseline FROM v2_expert_signals WHERE id=$3",
                uuid.uuid4(),message,first)
        distinct = await self.signal(line='3.5')
        item = (await self.listing())['items'][0]
        self.assertEqual(2, len(item['expert_signals']))
        self.assertIn(str(distinct), {s['id'] for s in item['expert_signals']})
        self.assertFalse(item['expert_signals_has_more'])

    async def test_native_kickoff_handicap_zero_equivalence_and_verified_total_rule(self):
        from app.orders import eligible_signal
        from fastapi import HTTPException
        await self.trusted()
        signal = await self.signal(phase='scheduled')
        await self.pool.execute("INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase,home_score,away_score) "
                                "VALUES('leyu:1234567','native-test','football','League','Home','Away',now(),'live',0,0)")
        await self.move_signal(signal, 'leyu:1234567', market='4', phase='scheduled')
        await self.pool.execute("UPDATE v2_expert_signals SET market_kind='handicap',score_basis='full_period'")
        await self.pool.execute("UPDATE v2_current_quotes SET market_kind='handicap',score_basis='unknown'")
        self.assertEqual(1, len((await self.listing())['items']))
        await self.pool.execute("UPDATE v2_fixtures SET home_score=1 WHERE id='leyu:1234567'")
        self.assertEqual([], (await self.listing())['items'])
        async with self.pool.acquire() as conn, conn.transaction():
            with self.assertRaises(HTTPException):
                await eligible_signal(conn, signal, manual=True)
        await self.pool.execute("UPDATE v2_expert_signals SET market_id='2',market_kind='total'")
        await self.pool.execute("UPDATE v2_current_quotes SET market_id='2',market_kind='total'")
        self.assertEqual(1, len((await self.listing())['items']))
        async with self.pool.acquire() as conn, conn.transaction():
            await eligible_signal(conn, signal, manual=True)

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
        await self.move_signal(basketball_signal, 'basketball', market='38', phase='scheduled')
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

    async def test_all_settled_outcomes_hide_fixture_without_deleting_history(self):
        await self.trusted()
        signal = await self.signal()
        for outcome in ('win', 'half_win', 'loss', 'half_loss', 'push', 'void'):
            with self.subTest(outcome=outcome):
                await self.pool.execute('UPDATE v2_expert_signals SET outcome=$2 WHERE id=$1', signal, outcome)
                self.assertEqual([], (await self.listing())['items'])
                all_matches = await self.client.get('/api/v1/public/matches')
                self.assertEqual(['fixture'], [item['id'] for item in all_matches.json()['items']])
                detail = await self.client.get('/api/v1/public/matches/fixture')
                self.assertEqual(200, detail.status_code, detail.text)
                self.assertEqual(outcome, detail.json()['expert_signals'][0]['outcome'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_mixed_markets_filter_before_guest_and_member_summary_limits(self):
        await self.trusted()
        pending = [await self.signal(line=str(i + 1)) for i in range(6)]
        for i in range(22):
            settled = await self.signal(line=str(i + 20))
            await self.pool.execute("UPDATE v2_expert_signals SET outcome='win' WHERE id=$1", settled)
        item = (await self.listing())['items'][0]
        self.assertEqual(5, len(item['expert_signals']))
        self.assertTrue(item['expert_signals_has_more'])
        self.assertEqual({'pending'}, {s['outcome'] for s in item['expert_signals']})
        with patch.object(public, '_has_entitlement', AsyncMock(return_value=True)):
            item = (await self.listing())['items'][0]
        self.assertEqual({str(identifier) for identifier in pending}, {s['id'] for s in item['expert_signals']})
        self.assertFalse(item['expert_signals_has_more'])
        await self.pool.execute("UPDATE v2_expert_signals SET outcome='push' WHERE id=$1", pending[0])
        item = (await self.listing())['items'][0]
        self.assertEqual(5, len(item['expert_signals']))
        self.assertFalse(item['expert_signals_has_more'])

    async def test_settlement_filters_fixtures_before_pagination_and_refresh(self):
        await self.trusted()
        settled = await self.signal()
        await self.pool.execute("UPDATE v2_expert_signals SET outcome='loss' WHERE id=$1", settled)
        for index in range(2):
            identifier = f'pending-{index}'
            await self.pool.execute(
                "INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase) "
                "VALUES($1,$1,'football','League','Home','Away',$2,'live')",
                identifier, self.now + timedelta(minutes=index + 1))
            signal = await self.signal(line=str(index + 3))
            await self.move_signal(signal, identifier)
        first = await self.listing(page_size=1)
        self.assertEqual(['pending-0'], [m['id'] for m in first['items']])
        self.assertTrue(first['next_cursor'])
        second = await self.listing(page_size=1, cursor=first['next_cursor'])
        self.assertEqual(['pending-1'], [m['id'] for m in second['items']])
        self.assertIsNone(second['next_cursor'])
        await self.pool.execute("UPDATE v2_expert_signals SET outcome='void' WHERE fixture_id='pending-0'")
        self.assertEqual(['pending-1'], [m['id'] for m in (await self.listing())['items']])

    async def test_finished_pending_market_is_hidden_without_settling_history(self):
        await self.trusted()
        signal = await self.signal()
        await self.pool.execute("UPDATE v2_fixtures SET phase='finished' WHERE id='fixture'")
        self.assertEqual([], (await self.listing(phase='finished'))['items'])
        detail = (await self.client.get('/api/v1/public/matches/fixture')).json()
        self.assertEqual('pending', detail['expert_signals'][0]['outcome'])
        self.assertEqual('pending', await self.pool.fetchval('SELECT outcome FROM v2_expert_signals WHERE id=$1', signal))

    async def test_old_scope_cursor_is_rejected_and_settlement_race_keeps_page_cursor(self):
        await self.trusted()
        await self.signal()
        scope = {'kind':'matches','phase':'all','sport':None,'league':None,'league_ids':[],
                 'q':None,'date':None,'tz':'Asia/Shanghai','recommended_only':True}
        old_cursor = public.encode_cursor(b'k'*32, scope, self.now, 'fixture')
        response = await self.client.get('/api/v1/public/matches', params={
            'recommended_only':'true','cursor':old_cursor})
        self.assertEqual(400, response.status_code)
        await self.pool.execute(
            "INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase) "
            "VALUES('later','later','football','League','Home','Away',$1,'live')", self.now + timedelta(minutes=1))
        later = await self.signal(line='3.5')
        await self.move_signal(later, 'later')
        with patch.object(public, '_recommendation_summaries', AsyncMock(return_value={})):
            raced = await self.listing(page_size=1)
            self.assertEqual([], raced['items'])
            self.assertTrue(raced['next_cursor'])
        after_race = await self.listing(page_size=1, cursor=raced['next_cursor'])
        self.assertEqual(['later'], [item['id'] for item in after_race['items']])

    async def test_ineligible_signal_conditions_are_hidden_before_order_reservation(self):
        from app.orders import eligible_signal
        from fastapi import HTTPException
        await self.trusted()
        signal = await self.signal()
        cases = [
            "UPDATE v2_expert_messages SET posted_at=now()+interval '6 seconds'",
            "UPDATE v2_expert_messages SET media_state='pending'",
            "UPDATE v2_expert_signals SET phase='scheduled'",
            "UPDATE v2_fixtures SET phase='finished'",
            "UPDATE v2_fixtures SET phase='cancelled'",
            "UPDATE v2_expert_signals SET market_kind='unsupported'",
            "UPDATE v2_current_quotes SET play_id=NULL",
            "UPDATE v2_current_quotes SET market_instance_id=''",
            "UPDATE v2_current_quotes SET selection_id=NULL",
        ]
        for sql in cases:
            with self.subTest(condition=sql):
                async with self.pool.acquire() as conn:
                    transaction = conn.transaction()
                    await transaction.start()
                    try:
                        await conn.execute(sql)
                        with self.assertRaises(HTTPException):
                            await eligible_signal(conn, signal, manual=True)
                        # Use this transaction's connection for the public read.
                        with patch.object(public.database, 'current_pool', return_value=conn):
                            self.assertEqual([], (await self.listing())['items'])
                    finally:
                        await transaction.rollback()
        self.assertEqual(1, len((await self.listing())['items']))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_exact_active_native_quote_required_without_cached_age_or_price_lock(self):
        await self.trusted()
        signal = await self.signal()
        cases = [
            "DELETE FROM v2_current_quotes",
            "UPDATE v2_current_quotes SET status='suspended'",
            "UPDATE v2_current_quotes SET status='closed'",
            "UPDATE v2_current_quotes SET source_updated_at=now()+interval '5 seconds'",
            "UPDATE v2_current_quotes SET price=NULL",
            "UPDATE v2_current_quotes SET price='NaN'::numeric",
            "UPDATE v2_current_quotes SET line=3.5",
            "UPDATE v2_current_quotes SET selection='under'",
            "UPDATE v2_current_quotes SET period='first_half'",
            "UPDATE v2_current_quotes SET market_id='other'",
            "UPDATE v2_current_quotes SET market_kind='handicap'",
        ]
        for sql in cases:
            with self.subTest(condition=sql):
                async with self.pool.acquire() as conn:
                    transaction = conn.transaction()
                    await transaction.start()
                    try:
                        await conn.execute(sql)
                        with patch.object(public.database, 'current_pool', return_value=conn):
                            self.assertEqual([], (await self.listing())['items'])
                    finally:
                        await transaction.rollback()
        # Changed odds keep the recommendation visible; the immutable original is evidence only.
        await self.pool.execute('UPDATE v2_current_quotes SET price=2.35')
        self.assertEqual(str(signal), (await self.listing())['items'][0]['expert_signals'][0]['id'])
        for timestamp in ("NULL", "now()-interval '10 minutes'"):
            await self.pool.execute(f"UPDATE v2_current_quotes SET source_updated_at={timestamp}")
            with patch.object(public, 'runtime_quote_max_age', AsyncMock(return_value=15)) as freshness:
                self.assertEqual(1, len((await self.listing())['items']))
                freshness.assert_not_awaited()

    async def test_older_pending_recommendation_remains_visible_for_manual_provider_repricing(self):
        from app.orders import eligible_signal
        from fastapi import HTTPException
        await self.trusted()
        signal = await self.signal()
        await self.pool.execute("UPDATE v2_expert_signals SET created_at=now()-interval '1 hour'")
        await self.pool.execute("UPDATE v2_expert_messages SET posted_at=now()-interval '1 hour'")
        await self.pool.execute("UPDATE v2_current_quotes SET source_updated_at=now()-interval '10 minutes'")
        self.assertEqual(str(signal), (await self.listing())['items'][0]['expert_signals'][0]['id'])
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                with self.assertRaises(HTTPException):
                    await eligible_signal(conn, signal)
                self.assertEqual(signal, (await eligible_signal(conn, signal, manual=True))['id'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))

    async def test_invalid_recommendations_do_not_consume_summary_limits_or_match_pages(self):
        from app.orders import eligible_signal
        await self.trusted()
        for i in range(22):
            invalid = await self.signal(line=str(i + 20))
            await self.pool.execute("UPDATE v2_expert_signals SET withdrawn_at=now() WHERE id=$1", invalid)
        self.assertEqual([], (await self.listing(page_size=1))['items'])
        valid = [await self.signal(line=str(i + 1)) for i in range(6)]
        item = (await self.listing(page_size=1))['items'][0]
        self.assertEqual(5, len(item['expert_signals']))
        self.assertTrue(item['expert_signals_has_more'])
        with patch.object(public, '_has_entitlement', AsyncMock(return_value=True)):
            item = (await self.listing())['items'][0]
        self.assertEqual({str(s) for s in valid}, {s['id'] for s in item['expert_signals']})
        self.assertFalse(item['expert_signals_has_more'])
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for identifier in valid:
                    self.assertEqual(identifier, (await eligible_signal(conn, identifier))['id'])

    async def test_winner_null_zero_and_supported_basketball_markets(self):
        from services.execution_scope import BASKETBALL_MARKETS, execution_market_supported
        await self.trusted()
        await self.signal()
        for market, (kind, period) in BASKETBALL_MARKETS.items():
            with self.subTest(market=market):
                await self.pool.execute("UPDATE v2_fixtures SET sport='basketball'")
                await self.pool.execute('UPDATE v2_expert_signals SET market_id=$1,market_kind=$2,period=$3,selection=$4,line=$5',
                    market, kind, period, 'home' if kind!='total' else 'over', None if kind=='winner' else Decimal('2.25'))
                await self.pool.execute('UPDATE v2_current_quotes SET market_id=$1,market_kind=$2,period=$3,selection=$4,line=$5',
                    market, kind, period, 'home' if kind!='total' else 'over', Decimal('0') if kind=='winner' else Decimal('2.25'))
                self.assertTrue(execution_market_supported('basketball', market, kind, period))
                self.assertEqual(1, len((await self.listing())['items']))
                for invalid in ("UPDATE v2_expert_signals SET period='first_half'",
                                "UPDATE v2_expert_signals SET selection='draw'",
                                "UPDATE v2_expert_signals SET market_id='unknown'"):
                    async with self.pool.acquire() as conn:
                        tx = conn.transaction()
                        await tx.start()
                        try:
                            await conn.execute(invalid)
                            with patch.object(public.database, 'current_pool', return_value=conn):
                                self.assertEqual([], (await self.listing())['items'])
                        finally:
                            await tx.rollback()
