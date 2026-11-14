import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from infra.monitor.window_audit import assemble,interval_health,duplicate_count,collector_failure_count,audit_directory


class WindowAuditTests(unittest.TestCase):
    def test_recreated_container_cannot_supply_predecessor_log_evidence(self):
        with tempfile.TemporaryDirectory()as name:
            root=Path(name);window=root/'1800';window.mkdir();(window/'independent').mkdir()
            (root/'health').mkdir();body=b'{"messages":[]}'
            snapshot=window/'snapshot.json';snapshot.write_bytes(body)
            summary=dict(start=1800,end=3600,snapshot_sha256=hashlib.sha256(body).hexdigest(),
                snapshot_file=str(snapshot),trusted_messages=0,missing_messages=0,
                upstream_complete=True,database_complete=True)
            (window/'snapshot-summary.json').write_text(json.dumps(summary))
            with patch('infra.monitor.window_audit.subprocess.run',return_value=SimpleNamespace(
                    returncode=0,stdout=b'1970-01-01T01:01:00Z'))as run:
                result=audit_directory(window)
            self.assertFalse(result['source_health_ok'])
            self.assertFalse(result['collector_log_complete'])
            self.assertEqual(1,run.call_count)

    def test_transient_collector_failure_is_counted_even_between_health_samples(self):
        log='Telegram recent history complete state=catching_up\n'+\
            'Telegram source deferred category=history_deadline stage=archive_page\n'+\
            'Telegram source lease refresh deferred category=TimeoutError\n'+\
            'Telegram recent history complete state=caught_up'
        self.assertEqual(2,collector_failure_count(log))
        self.assertEqual(0,collector_failure_count('Telegram live event persisted'))

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
        self.assertEqual(1,duplicate_count([{'signals':[signal,dict(signal,line='2.500')]}]))
        self.assertEqual(0,duplicate_count([{'signals':[signal,dict(signal,withdrawn_at='now')]}]))

    def test_retry_cannot_finish_before_original_deadline_is_also_a_failure(self):
        summary=dict(start=1800,end=3600,snapshot_sha256='exact',trusted_messages=1,
            missing_messages=0,upstream_complete=True,database_complete=True)
        snapshot={'messages':[dict(id='m',review_mode='real_time',
            last_error='retry_exceeds_detection_deadline')]}
        ref=hashlib.sha256(b'm').hexdigest()[:12]
        audit=dict(snapshot_sha256='exact',classification='recommendation',verdict='consistent')
        value=assemble(summary,snapshot,{ref:audit},[])
        self.assertEqual(1,value['deadline_violations'])
