import hashlib
import unittest
from infra.monitor.window_audit import assemble,interval_health,duplicate_count


class WindowAuditTests(unittest.TestCase):
    def test_health_requires_interval_coverage_and_no_failed_sample(self):
        samples=[{'at':n,'ok':True}for n in range(1800,3661,60)]
        self.assertTrue(interval_health(samples,1800,3600))
        self.assertFalse(interval_health(samples[4:],1800,3600))
        self.assertFalse(interval_health(samples[:5]+samples[9:],1800,3600))
        self.assertFalse(interval_health(samples+[{'at':1900,'ok':False}],1800,3600))

    def test_wrong_snapshot_unknown_semantics_and_deadline_cannot_pass(self):
        summary=dict(start=1800,end=3600,snapshot_sha256='exact',trusted_messages=1,
            missing_messages=0,upstream_complete=True,database_complete=True)
        snapshot={'messages':[dict(id='m',review_mode='real_time',last_error='detection_deadline_exceeded')]}
        ref=hashlib.sha256(b'm').hexdigest()[:12]
        audit=dict(snapshot_sha256='wrong',classification='recommendation',verdict='consistent')
        value=assemble(summary,snapshot,{ref:audit},[])
        self.assertEqual(1,value['unreviewed_messages']);self.assertEqual(1,value['deadline_violations'])
        audit.update(snapshot_sha256='exact',verdict='uncertain')
        value=assemble(summary,snapshot,{ref:audit},[])
        self.assertEqual(0,value['unreviewed_messages']);self.assertEqual(1,value['unexplained_outcomes'])
        self.assertFalse(value['source_health_ok'])

    def test_withdrawn_duplicate_is_not_double_counted_but_active_repeat_is(self):
        signal=dict(expert_id='e',fixture_id='f',market_id='m',market_kind='total',period='full_time',
            selection='over',line='2.5',phase='scheduled',score_basis='full_period')
        self.assertEqual(1,duplicate_count([{'signals':[signal,dict(signal)]}]))
        self.assertEqual(0,duplicate_count([{'signals':[signal,dict(signal,withdrawn_at='now')]}]))
