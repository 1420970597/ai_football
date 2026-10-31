from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from app.quote_observation import confirmation_for, DETAIL_PATH
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
