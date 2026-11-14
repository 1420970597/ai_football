import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parents[1]/'infra/monitor'))
import audit_runner

class AuditBoundedConcurrencyTests(unittest.TestCase):
    def test_two_reads_overlap_and_existing_uncertainty_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            directory=Path(folder)
            destination=directory/'independent';destination.mkdir()
            frozen=destination/(hashlib.sha256(b'old').hexdigest()[:12]+'.json')
            frozen.write_text(json.dumps({'verdict':'uncertain'}))
            messages=[{'id':v}for v in ('old','1','2','3','4')]
            guard=threading.Lock();together=threading.Event()
            active=maximum=0
            def review(source,message):
                nonlocal active,maximum
                with guard:
                    active+=1;maximum=max(maximum,active)
                    if active==2:together.set()
                try:
                    if not together.wait(1):raise RuntimeError('no_overlap')
                    time.sleep(.01)
                    return {'verdict':'consistent'}
                finally:
                    with guard:active-=1
            with patch.object(audit_runner,'pending_messages',return_value=({'start':0,'snapshot_sha256':'bound'},messages)), patch.object(audit_runner,'review_message',side_effect=review) as work, patch.object(audit_runner,'audit_directory',return_value={'audit_complete':True,'unexplained_outcomes':1}) as assemble:
                audit_runner.audit_window(directory,directory)
            self.assertEqual(2,maximum);self.assertEqual(0,active)
            self.assertEqual(4,work.call_count)
            self.assertEqual({'verdict':'uncertain'},json.loads(frozen.read_text()))
            self.assertEqual(5,len(list(destination.glob('*.json'))))
            self.assertTrue((directory/'audit.json').exists())
            assemble.assert_called_once_with(directory)

    def test_transport_failure_keeps_other_results_and_cannot_seal(self):
        with tempfile.TemporaryDirectory() as folder:
            directory=Path(folder)
            def review(source,message):
                if message['id']=='failed':raise RuntimeError('network')
                return {'verdict':'uncertain'}
            with patch.object(audit_runner,'pending_messages',return_value=({'start':0,'snapshot_sha256':'bound'},[{'id':'failed'},{'id':'valid'}])), patch.object(audit_runner,'review_message',side_effect=review), patch.object(audit_runner,'audit_directory') as assemble:
                with self.assertRaisesRegex(RuntimeError,'audit_transport_incomplete'):
                    audit_runner.audit_window(directory,directory)
            self.assertFalse((directory/'audit.json').exists())
            self.assertEqual(1,len(list((directory/'independent').glob('*.json'))))
            assemble.assert_not_called()
