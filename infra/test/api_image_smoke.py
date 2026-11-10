"""Run inside the API image, with no credentials, network or writable root."""
import importlib
import hashlib
import json
import os
from pathlib import Path


assert os.geteuid() == 10001, "image must use its production service account"
import quote_witness
witness_path = Path(quote_witness.__file__)
assert witness_path == Path('/app/quote_witness.py')
witness_sha256 = hashlib.sha256(witness_path.read_bytes()).hexdigest()
assert witness_sha256 == os.environ['EXPECTED_QUOTE_WITNESS_SHA256'], "shared protocol source must match the frozen build input"
shared_hashes = {}
for name in ('execution_scope', 'basketball_scores'):
    module = importlib.import_module(name)
    path = Path(module.__file__)
    assert path == Path('/app') / (name + '.py')
    shared_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert shared_hashes[name] == os.environ['EXPECTED_' + name.upper() + '_SHA256']
checked = 0
for directory, suffix in ((Path('/app/app'), '.py'), (Path('/app/migrations'), '.sql')):
    assert directory.is_dir() and os.access(directory, os.R_OK | os.X_OK)
    paths = sorted(directory.rglob('*' + suffix))
    assert paths, "runtime source must be enumerable by the service account"
    for path in paths:
        path.read_bytes()
        checked += 1
for name in ('app.main', 'app.worker', 'app.migrate'):
    importlib.import_module(name)
from app.database import migration_files

definitions = migration_files()
assert definitions and all(content.strip() for _, _, content in definitions)
print(json.dumps({'uid': os.geteuid(), 'readable_source_files': checked,
                  'quote_witness_sha256': witness_sha256,
                  'shared_sports_modules_sha256': shared_hashes,
                  'migration_count': len(definitions), 'api_worker_migrate_imported': True,
                  'credentials_supplied': False, 'network_access': False}))
