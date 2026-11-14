import json
from pathlib import Path
import tempfile
import unittest
from infra.monitor.private_retention import prune

class PrivateRetentionTests(unittest.TestCase):
    def manifest(self,root,**extra):
        value=dict(earliest_original_posted_at='2026-11-11T12:00:00Z',expires_at='2026-11-14T12:00:00Z',files=['source.private.json'])
        value.update(extra);(root/'raw-retention-manifest.json').write_text(json.dumps(value))

    def test_original_ttl_preserves_unknown_files_and_ignores_mtime(self):
        with tempfile.TemporaryDirectory()as d:
            root=Path(d);raw=root/'source.private.json';raw.write_text('private')
            other=root/'model.bin';other.write_text('not-registered');self.manifest(root)
            self.assertEqual(0,prune(root,1794657599));self.assertTrue(raw.exists())
            self.assertEqual(1,prune(root,1794657600));self.assertFalse(raw.exists());self.assertTrue(other.exists())

    def test_path_escape_symlink_and_extended_ttl_rejected(self):
        with tempfile.TemporaryDirectory()as d:
            root=Path(d)
            for extra in ({'files':['../outside']},{'expires_at':'2026-11-15T12:00:00Z'}):
                self.manifest(root,**extra)
                with self.assertRaises(ValueError):prune(root,2000000000)
            (root/'source.private.json').symlink_to('/etc/hosts');self.manifest(root)
            with self.assertRaises(ValueError):prune(root,2000000000)

    def test_existing_image_cutoff_is_honored_without_renewal(self):
        with tempfile.TemporaryDirectory()as d:
            root=Path(d);(root/'source.png').write_bytes(b'private')
            (root/'image.manifest.json').write_text(json.dumps({
                'expires_at':'2026-11-14T12:00:00Z','files':['source.png']}))
            self.assertEqual(1,prune(root,1794657600))
