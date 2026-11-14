import importlib.util
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('save_window',Path(__file__).parents[1]/'infra/monitor/save_window.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class SaveWindowTests(unittest.TestCase):
    def value(self):
        end=datetime.now(timezone.utc)-timedelta(minutes=1)
        return {'start':(end-timedelta(minutes=30)).isoformat(),'end':end.isoformat(),
                'messages':[{'posted_at':(end-timedelta(hours=2)).isoformat(),'raw_text':'private-original'}],
                'source_evidence':[{'complete':True,'trusted_messages':1,'missing_messages':0}],
                'database_complete':True}

    def test_manifest_uses_original_post_not_export_time_and_summary_has_no_originals(self):
        value=self.value()
        with tempfile.TemporaryDirectory()as name:
            summary=module.save(Path(name),json.dumps(value).encode())
            path=Path(summary['snapshot_file'])
            manifest=json.loads(path.with_name(path.name+'.manifest.json').read_text())
            expected=datetime.fromisoformat(value['messages'][0]['posted_at'])+timedelta(hours=72)
            self.assertEqual(expected.isoformat(),manifest['expires_at'])
            self.assertEqual(0o600,path.stat().st_mode&0o777)
            self.assertNotIn('private-original',json.dumps(summary))
            self.assertTrue(summary['upstream_complete'])

    def test_future_or_expired_original_cannot_be_saved(self):
        for mode in ('future','expired'):
            value=self.value()
            if mode=='future':value['end']=(datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat()
            else:value['messages'][0]['posted_at']=(datetime.now(timezone.utc)-timedelta(hours=73)).isoformat()
            with tempfile.TemporaryDirectory()as name,self.assertRaises(ValueError):
                module.save(Path(name),json.dumps(value).encode())
