from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import unittest
import uuid

from app.review_period_intent import period_intent_rejection, verified_early_first_half_context
import test_reviews as support


def quote_context(now, **changes):
    value = {'market_kind':'total','line':Decimal('1.75'),'selection':'over','period':'first_half',
             'fixture_id':'leyu:5783693','source_id':'leyu:5783693','phase':'live',
             'fixture_sport':'football','fixture_match_state':'6','fixture_clock_minute':22,
             'fixture_received_at':now-timedelta(seconds=26),
             'intent_candidate':{'id':'leyu:5783693','source_id':'leyu:5783693','sport':'football',
                 'phase':'live','match_state':'6','clock_minute':22,
                 'received_at':(now-timedelta(seconds=68)).isoformat()}}
    value.update(changes)
    return value


class DelayedHalfContextTests(unittest.TestCase):
    def test_fresh_real_stage_after_two_minutes_uses_existing_message_deadline(self):
        now=datetime.now(timezone.utc);posted=now-timedelta(seconds=261)
        for line in (Decimal('1.75'),Decimal('1.5')):
            quote=quote_context(now,line=line)
            self.assertIsNone(period_intent_rejection('半大1.75 1.5',quote,posted_at=posted,now=now))
            context=verified_early_first_half_context('半大1.75 1.5',quote,posted_at=posted,now=now)
            self.assertEqual('leyu-early-first-half-stage-v2',context['algorithm'])
            self.assertEqual(300,context['source_age_limit_seconds'])
            self.assertEqual(posted.isoformat(),context['message_posted_at'])
            quote['period']='full_time'
            self.assertEqual('source_period_intent_conflict',
                period_intent_rejection('半大1.75 1.5',quote,posted_at=posted,now=now))

    def test_late_review_does_not_extend_any_source_or_snapshot_deadline(self):
        now=datetime.now(timezone.utc);posted=now-timedelta(seconds=261)
        cases=[(quote_context(now),now-timedelta(seconds=301)),
               (quote_context(now,fixture_received_at=now-timedelta(seconds=121)),posted),
               (quote_context(now,fixture_match_state='7',fixture_clock_minute=46),posted),
               (quote_context(now,fixture_received_at=now+timedelta(seconds=1)),posted)]
        stale=quote_context(now);stale['intent_candidate']['received_at']=(now-timedelta(seconds=181)).isoformat()
        cases.append((stale,posted))
        early=quote_context(now);early['intent_candidate']['received_at']=(posted-timedelta(seconds=61)).isoformat()
        cases.append((early,posted))
        for quote,source_at in cases:
            with self.subTest(quote=quote,source_at=source_at):
                self.assertEqual('source_period_intent_ambiguous',
                    period_intent_rejection('半大1.75 1.5',quote,posted_at=source_at,now=now))
                self.assertIsNone(verified_early_first_half_context('半大1.75 1.5',quote,posted_at=source_at,now=now))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires task-owned isolated database')
class DelayedHalfRecordingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    fixture=support.ReviewTests.fixture
    post=support.ReviewTests.post
    context=support.ReviewTests.context
    search_all=support.ReviewTests.search_all
    resolve=support.ReviewTests.resolve
    _reset_job_posted_at=support.ReviewTests._reset_job_posted_at

    async def test_delayed_current_half_is_recorded_with_versioned_evidence(self):
        posted=self.now-timedelta(seconds=250)
        await self._reset_job_posted_at(posted)
        await self.pool.execute("UPDATE v2_expert_messages SET raw_text='半大1.75 1.5' WHERE id=$1",self.message)
        fixture=await self.fixture(id='leyu:5783693',phase='live',kickoff_at=self.now-timedelta(minutes=24),
            match_state='6',clock_minute=22,home_score=1,away_score=0,half_home_score=1,half_away_score=0,
            markets=[{'market_id':'18','market_name':'上半场大小','kind':'total','period':'first_half',
                'line':line,'selection':'over','price':'1.95','source_updated_at':self.now.isoformat(),
                'received_at':self.now.isoformat(),'score_basis':'full_period','play_id':'18',
                'market_instance_id':'half-'+line,'selection_id':'over-'+line}for line in('1.75','1.5')])
        key,_=await self.search_all();await self.resolve(key)
        read=await self.post('list_match_markets',fixture_id=fixture)
        self.assertEqual(200,read.status_code,read.text)
        response=await self.post('record_expert_signals',signals=[{'fixture_id':fixture,
            'quote_id':quote['quote_id'],'confidence':.8,'evidence_text':'半大1.75 1.5'}for quote in read.json()['markets']])
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual(2,len(response.json()['accepted']),response.text)
        self.assertEqual([],response.json()['rejected'])
        for accepted in response.json()['accepted']:
            context=await self.pool.fetchval("SELECT evidence->'source_period_context' FROM v2_quote_evidence "
                "WHERE reference_id=$1 AND evidence_kind='expert_signal'",uuid.UUID(accepted['signal_id']))
            self.assertEqual('leyu-early-first-half-stage-v2',context['algorithm'])
            self.assertEqual(300,context['source_age_limit_seconds'])
            self.assertEqual(posted.isoformat(),context['message_posted_at'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))
