from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch

from app.quote_observation import confirmation_for, confirmations_for, DETAIL_PATH
from app import quote_observation
from app.review_state import fresh_quote


class QuoteObservationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.fixture = {'id':'leyu:match','source_id':'leyu:match','phase':'scheduled',
                        'home_score':0,'away_score':0,'half_home_score':None,'half_away_score':None}
        self.quote = {'market_id':'4','market_kind':'handicap','period':'full_time','line':Decimal('-.5'),
                      'selection':'home','price':Decimal('1.95000'),'source_updated_at':self.now-timedelta(hours=1),
                      'status':'active','play_id':'4','market_instance_id':'instance','selection_id':'option','score_basis':'full_period'}
        wire = {**self.quote, 'line':'-0.5','price':'1.95','source_updated_at':self.quote['source_updated_at'].isoformat()}
        self.observation = {'path':DETAIL_PATH,'started_at':(self.now-timedelta(seconds=2)).isoformat(),
            'completed_at':(self.now-timedelta(seconds=1)).isoformat(), 'fixture_id':'leyu:match','source_match_id':'match',
            'context':{k:v for k,v in self.fixture.items() if k not in {'id','source_id'}},'quotes':[wire]}

    def confirm(self, observation=None, quote=None, fixture=None):
        return confirmation_for(observation or self.observation, quote or self.quote, fixture or self.fixture, 15, self.now)

    def test_native_team_total_side_identity_is_required_for_current_confirmation(self):
        fixture=self.fixture|{'id':'leyu:5715236','source_id':'leyu:5715236'}
        quote=self.quote|{'market_kind':'team_total','market_id':'10','play_id':'10','selection':'over','line':Decimal('1.75')}
        observation=self.observation|{'fixture_id':fixture['id'],'source_match_id':'5715236',
            'quotes':[quote|{'source_updated_at':quote['source_updated_at'].isoformat()}]}
        self.assertIsNotNone(self.confirm(observation,quote,fixture))
        for change in ({'play_id':'11'},{'market_id':'87','play_id':'87'},{'period':'first_half'}):
            bad=quote|change
            self.assertIsNone(self.confirm(observation|{'quotes':[bad]},bad,fixture))

    def test_independent_exact_current_observation_preserves_old_source_time_and_order_gate(self):
        result = self.confirm()
        self.assertIsNotNone(result)
        self.assertEqual(64, len(result['sha256']))
        self.assertFalse(fresh_quote(self.quote, 15, self.now))
        self.assertEqual(self.quote['source_updated_at'].isoformat(), result['quotes'][0]['source_updated_at'])
        self.assertIsNotNone(self.confirm(result))

    def test_unknown_future_or_naive_quote_time_never_confirmed(self):
        for time in (None, self.now+timedelta(seconds=1), self.now.replace(tzinfo=None)):
            with self.subTest(time=time):
                self.assertIsNone(self.confirm(quote={**self.quote,'source_updated_at':time}))

    def test_expired_future_reversed_missing_or_wrong_endpoint_observation_rejected(self):
        for change in ({'started_at':(self.now-timedelta(seconds=16)).isoformat()},
                       {'completed_at':(self.now+timedelta(seconds=1)).isoformat()},
                       {'completed_at':(self.now-timedelta(seconds=3)).isoformat()},
                       {'path':'/another'}, {'fixture_id':'another'}, {'source_match_id':'another'},
                       {'started_at':None}, {'started_at':self.now.replace(tzinfo=None).isoformat()}):
            with self.subTest(change=change):
                self.assertIsNone(self.confirm({**self.observation, **change}))

    def test_each_changed_quote_field_rejects_confirmation(self):
        changes = {'market_id':'19','market_kind':'total','period':'half_time','line':Decimal('-.75'),
            'selection':'away','price':Decimal('1.96'),'source_updated_at':self.now-timedelta(minutes=1),
            'status':'suspended','play_id':'19','market_instance_id':'new','selection_id':'new','score_basis':'remaining'}
        for key,value in changes.items():
            with self.subTest(key=key):
                self.assertIsNone(self.confirm(quote={**self.quote,key:value}))

    def test_score_phase_and_source_identity_changes_rejected(self):
        for key,value in [('phase','live'),('phase','finished'),('home_score',1),('away_score',1),
                          ('half_home_score',0),('half_away_score',0),('id','another'),('source_id','another')]:
            with self.subTest(key=key,value=value):
                self.assertIsNone(self.confirm(fixture={**self.fixture,key:value}))

    def test_duplicate_selected_quote_rejected_but_unrelated_unknown_quote_does_not_hide_valid_one(self):
        invalid = {**self.observation['quotes'][0],'source_updated_at':None}
        self.assertIsNotNone(self.confirm({**self.observation,'quotes':[invalid,*self.observation['quotes']]}))
        self.assertIsNone(self.confirm({**self.observation,'quotes':self.observation['quotes']*2}))

    def test_status_and_missing_native_identifiers_cannot_be_confirmed(self):
        for change in ({'status':'closed'},{'status':'suspended'},{'selection_id':''},{'market_instance_id':''},
                       {'play_id':''},{'market_kind':'unsupported'},{'price':None}):
            self.assertIsNone(self.confirm(quote={**self.quote,**change}))

    def assert_batch_equal(self, observation, quotes=None, fixture=None):
        quotes = quotes or [self.quote | {'id': 'q'}]
        fixture = fixture or self.fixture
        before = deepcopy(observation)
        expected = {q['id']: confirmation_for(observation,q,fixture,15,self.now) for q in quotes}
        actual = confirmations_for(observation,quotes,fixture,15,self.now)
        self.assertEqual(expected,actual)
        self.assertEqual(before,observation)
        return actual

    def test_batch_exact_witness_duplicate_invalid_and_malformed_identity_parity(self):
        self.assertIsNotNone(self.assert_batch_equal(self.observation)['q'])
        invalid = self.observation['quotes'][0] | {'source_updated_at': None}
        for entries in ([],[invalid,*self.observation['quotes']],self.observation['quotes']*3,
                        [self.observation['quotes'][0] | {'market_id': ['malformed']},*self.observation['quotes']],
                        self.observation['quotes']*2001):
            with self.subTest(count=len(entries)):
                self.assert_batch_equal(self.observation | {'quotes': entries})
        self.assert_batch_equal(self.observation, [self.quote | {'id':'bad','market_id':['malformed']}])
        for observation in (None,{},self.observation | {'quotes': None},self.observation | {'path':'wrong'},
                            self.observation | {'completed_at':(self.now+timedelta(seconds=1)).isoformat()},
                            self.observation | {'started_at':(self.now-timedelta(seconds=16)).isoformat()}):
            with self.subTest(observation=observation):
                self.assert_batch_equal(observation)

    def test_batch_keeps_changed_quote_and_fixture_rejections(self):
        for change in ({'price':Decimal('2.01')},{'source_updated_at':None},{'line':Decimal('-1')},
                       {'status':'closed'},{'selection_id':''},{'market_kind':'unsupported'},
                       {'source_updated_at':self.now.replace(tzinfo=None)}):
            self.assertIsNone(self.assert_batch_equal(self.observation,[self.quote | {'id':'q',**change}])['q'])
        for change in ({'phase':'finished'},{'home_score':1},{'source_id':'other'}):
            self.assertIsNone(self.assert_batch_equal(self.observation,fixture=self.fixture | change)['q'])

    def test_batch_2000_candidates_normalize_linearly_and_keep_every_witness(self):
        quotes=[self.quote | {'id':str(i),'selection_id':str(i)} for i in range(2000)]
        observation=self.observation | {'quotes':[self.observation['quotes'][0] | {'selection_id':str(i)} for i in range(2000)]}
        with patch.object(quote_observation,'_identity',wraps=quote_observation._identity) as identity:
            actual=confirmations_for(observation,quotes,self.fixture,15,self.now)
        self.assertEqual(2000,len(actual))
        self.assertTrue(all(actual.values()))
        self.assertLessEqual(identity.call_count,8000)
        for index in (0,999,1999):
            self.assertEqual(confirmation_for(observation,quotes[index],self.fixture,15,self.now),actual[str(index)])

    def test_batch_rechecks_expiration_after_indexing_instead_of_freezing_admission_time(self):
        clock=[self.now]
        class AdvancingDateTime(datetime):
            @classmethod
            def now(cls,tz=None):
                return clock[0]
        original=quote_observation._identity
        def normalize(value):
            clock[0]=self.now+timedelta(seconds=16)
            return original(value)
        with patch.object(quote_observation,'datetime',AdvancingDateTime), \
             patch.object(quote_observation,'_identity',side_effect=normalize):
            result=confirmations_for(self.observation,[self.quote | {'id':'q'}],self.fixture,15)
        self.assertIsNone(result['q'])
