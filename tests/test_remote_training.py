import hashlib
import json
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from tests.test_model_validation import approved_artifact
from service.remote_training import (REMOTE_TRAINING_THRESHOLD_BYTES, dispatch,
                                     estimated_temp_bytes, remote_training_required)


class RemoteTrainingTests(unittest.TestCase):
    def model(self):
        return approved_artifact(version='remote-v1')

    def test_threshold_requires_remote_execution(self):
        self.assertTrue(remote_training_required(REMOTE_TRAINING_THRESHOLD_BYTES + 1))
        self.assertFalse(remote_training_required(REMOTE_TRAINING_THRESHOLD_BYTES))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'ledger'
            (root.parent / 'models').mkdir(parents=True)
            (root.parent / 'models' / 'training-estimate.json').write_text(
                json.dumps({'peak_temp_bytes': 123}))
            self.assertEqual(estimated_temp_bytes(root), 123)

    def test_remote_response_is_validated_before_return(self):
        model = self.model()
        encoded = json.dumps(model, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        response = json.dumps({'model': model, 'sha256': hashlib.sha256(encoded).hexdigest()})
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {'MODEL_REMOTE_TRAINING_COMMAND': 'remote-wrapper'}, clear=False), \
                patch('service.remote_training.subprocess.run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = response
            run.return_value.stderr = ''
            result = dispatch(Path(directory) / 'ledger', cycle=100, cpu=1,
                              memory_mb=2048, estimate_bytes=REMOTE_TRAINING_THRESHOLD_BYTES + 1)
            self.assertEqual(result['version'], 'remote-v1')
            run.assert_called_once()

    def test_missing_bad_checksum_and_incompatible_models_are_rejected(self):
        from service.remote_training import _model_from_response
        model = self.model()
        for response in ({'model':model}, {'model':model,'sha256':'0'*64},
                         {'model':{'version':'old'},'sha256':'0'*64}):
            with self.assertRaises(ValueError):
                _model_from_response(response,Path('/work/output/ledger'))

    def test_remote_candidate_publication_uses_completion_and_current_promotion_rules(self):
        from service.model_training import publish_remote_model
        model = self.model()
        mids = {str(i) for i in range(120)}
        model.update(cohort_ids=sorted(mids), cohort_cutoff='2025-02-01T00:00:00+00:00',
                     promotion_evidence=model['promotion']['evidence'])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = publish_remote_model(root, model, {'cycle_matches':100},set(),mids,
                model['cohort_cutoff'],{'cpu_cores':1,'memory_mb':2048},101*1024**3)
            self.assertEqual(status['state'],'ready')
            self.assertEqual(len(json.loads((root/'completed-training.json').read_text())['cohort_ids']),120)
            self.assertTrue((root/'active.json').exists())
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError,'冻结边界'):
                publish_remote_model(Path(directory),model,{'cycle_matches':100},set(),{'outside'},
                    model['cohort_cutoff'],{},101*1024**3)
            self.assertFalse((Path(directory)/'active.json').exists())

    def test_remote_cycle_accepts_50_new_matches_but_requires_100_total(self):
        from service.model_training import publish_remote_model
        model = self.model()
        total = {str(i) for i in range(100)}
        model.update(cohort_ids=sorted(total), cohort_cutoff='2025-02-01T00:00:00+00:00',
                     promotion_evidence=model['promotion']['evidence'])
        consumed = {str(i) for i in range(50)}
        with tempfile.TemporaryDirectory() as directory:
            status = publish_remote_model(Path(directory), model, {'cycle_matches': 50}, consumed,
                                          total, model['cohort_cutoff'], {}, 101 * 1024**3)
            self.assertEqual(status['completed_matches'], 100)
        with tempfile.TemporaryDirectory() as directory:
            small = {str(i) for i in range(99)}
            model['cohort_ids'] = sorted(small)
            with self.assertRaisesRegex(ValueError, '冻结边界'):
                publish_remote_model(Path(directory), model, {'cycle_matches': 50},
                                     {str(i) for i in range(49)}, small,
                                     model['cohort_cutoff'], {}, 101 * 1024**3)

    def test_sqlite_space_is_included_in_automatic_estimate(self):
        from service.remote_training import local_pipeline_estimate
        self.assertEqual(local_pipeline_estimate(256,sqlite_bytes=100),local_pipeline_estimate(256)+200)

    def test_missing_estimate_is_unknown_and_invalid_estimate_blocks(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{},clear=True):
            self.assertIsNone(estimated_temp_bytes(Path(directory)/'ledger'))
            for value in ('0','-1','invalid'):
                with patch.dict(os.environ,{'MODEL_TRAINING_ESTIMATED_TEMP_BYTES':value}):
                    with self.assertRaises(ValueError):
                        estimated_temp_bytes(Path(directory)/'ledger')

    def test_artifact_path_escape_is_rejected(self):
        from service.remote_training import _model_from_response
        with self.assertRaisesRegex(ValueError,'路径'):
            _model_from_response({'artifact_path':'/etc/passwd','sha256':'0'*64},Path('/work/output/ledger'))

    def test_missing_command_does_not_allow_local_fallback(self):
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaisesRegex(RuntimeError,'已阻止本地训练'):
                dispatch(Path('/work/output/ledger'),cycle=100,cpu=1,memory_mb=2048,
                         estimate_bytes=REMOTE_TRAINING_THRESHOLD_BYTES+1)


if __name__ == '__main__':
    unittest.main()
