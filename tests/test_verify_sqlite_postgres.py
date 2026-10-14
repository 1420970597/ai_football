from contextlib import closing, redirect_stdout
import copy
import io
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from tools.verify_sqlite_postgres import (_bounded_verify_sql, _require_target_index,
    _reconcile_metadata, _source_signature, _stage_names, _write_report, cleanup_resumable, fingerprint, fingerprint_sql,
    verify_resumable)


class _Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def close(self):
        pass


class _StatementTimeout(Exception):
    sqlstate = '57014'


class _Copy:
    def __init__(self, db, table):
        self.db, self.table = db, table

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def write_row(self, row):
        if self.db.fail_copy_after is not None and self.db.copy_rows >= self.db.fail_copy_after:
            self.db.fail_copy_after = None
            raise ConnectionError('COPY interrupted after committed batch')
        if self.db.fail_copy:
            self.db.fail_copy = False
            raise ConnectionError('COPY connection lost')
        self.db.data['stages'][self.table][row[0]] = tuple(row)
        self.db.copy_rows += 1


class _Cursor:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def copy(self, sql):
        return _Copy(self.db, re.search(r'COPY "([^"]+)"', sql).group(1))


class _FakePG:
    """Transactional stage/checkpoint fake; no network or real PG scans."""
    def __init__(self, targets, data=None):
        self.targets = targets
        self.data = data if data is not None else {'stages': {}, 'states': {}}
        self._connection = self
        self.snapshot = None
        self.fail_copy = False
        self.fail_copy_after = None
        self.fail_checkpoint = False
        self.fail_after_verified = None
        self.timeout_queries = 0
        self.copy_rows = 0
        self.verify_calls = 0
        self.commits = self.rollbacks = 0
        self.calls = []
        self.plan = [{'Plan': {'Node Type': 'Nested Loop', 'Plans': [
            {'Node Type': 'Index Scan', 'Relation Name': 'model_inputs',
             'Alias': 't', 'Index Cond': 'match_id=s.match_id AND at=s.at AND algorithm=s.algorithm '
             'AND market=s.market AND line=s.line AND outcome=s.outcome'}]}}]

    def cursor(self):
        return _Cursor(self)

    def __enter__(self):
        self.snapshot = copy.deepcopy(self.data)
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()

    def commit(self):
        self.snapshot = None
        self.commits += 1

    def rollback(self):
        if self.snapshot is not None:
            self.data.clear()
            self.data.update(self.snapshot)
            self.snapshot = None
        self.rollbacks += 1

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        table = re.search(r'"(migration_verify_[^"]+)"', sql)
        name = table.group(1) if table else ''
        if sql.startswith('SELECT pg_'):
            return _Result([(True,)])
        if sql.startswith('SET '):
            return _Result()
        if sql.startswith('CREATE UNLOGGED'):
            bucket = self.data['states'] if name.endswith('_state') else self.data['stages']
            bucket.setdefault(name, {} if not name.endswith('_state') else None)
            return _Result()
        if sql.startswith('SELECT payload'):
            state = self.data['states'].get(name)
            return _Result([(state,)] if state else [])
        if sql.startswith('SELECT 1 FROM'):
            return _Result([(1,)] if self.data['stages'][name] else [])
        if sql.startswith('SELECT source_rowid'):
            return _Result([(params[0],)] if params[0] in self.data['stages'][name] else [])
        if sql.startswith('INSERT INTO'):
            state = json.loads(params[0])
            if self.fail_checkpoint and state['copied_rows']:
                self.fail_checkpoint = False
                raise ConnectionError('checkpoint connection lost')
            self.data['states'][name] = params[0]
            return _Result()
        if sql.startswith('EXPLAIN'):
            return _Result([(self.plan,)])
        if sql.startswith('WITH batch'):
            self.verify_calls += 1
            if self.timeout_queries:
                self.timeout_queries -= 1
                raise _StatementTimeout('statement timeout')
            if self.fail_after_verified is not None and self.verify_calls > self.fail_after_verified:
                raise ConnectionError('verification connection lost')
            after, _, limit = params
            rows = [row for row_id, row in sorted(self.data['stages'][name].items())
                    if after is None or row_id > after][:limit]
            missing = mismatches = 0
            for row in rows:
                target = self.targets.get(tuple(row[1:-2]))
                if target is None:
                    missing += 1
                elif target != row[-2]:
                    mismatches += 1
            return _Result([{'source_rows': len(rows), 'last_rowid': rows[-1][0] if rows else None,
                'missing_rows': missing, 'content_mismatches': mismatches, 'lagging_revisions': 0}])
        if sql.startswith('DROP TABLE'):
            bucket = self.data['states'] if name.endswith('_state') else self.data['stages']
            bucket.pop(name)
            return _Result()
        raise AssertionError('unexpected SQL: '+sql)


class ResumableVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd(), prefix='verify-resume-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'inputs.sqlite3'
        self.rows = [('m1','2026-01-01','algo','OU','2.5','over',.55,'[0.1,0.2]'),
                     ('m2','2026-01-02','algo','AH','0','home',.65,'[0.3,0.4]'),
                     ('m3','2026-01-03','other','OU','3','under',.75,'[0.5,0.6]')]
        with closing(sqlite3.connect(self.path)) as source, source:
            source.execute('CREATE TABLE inputs(match_id TEXT,at TEXT,algorithm TEXT,market TEXT,line TEXT,'
                           'outcome TEXT,baseline REAL,x TEXT,PRIMARY KEY(match_id,at,algorithm,market,line,outcome))')
            for rowid, row in zip((-2, 3, 7), self.rows):
                source.execute('INSERT INTO inputs(_rowid_,match_id,at,algorithm,market,line,outcome,baseline,x) '
                               'VALUES (?,?,?,?,?,?,?,?,?)', (rowid, *row))
        kinds = ['TEXT']*6+['REAL','TEXT']
        self.targets = {tuple(row[:6]): bytes.fromhex(fingerprint(row, kinds)) for row in self.rows}

    def verify(self, db, **kwargs):
        with redirect_stdout(io.StringIO()):
            return verify_resumable(db, self.root, self.path.name, 'inputs', 'model_inputs',
                                    copy_batch=2, verify_batch=2, **kwargs)

    def metadata_evidence(self, *, wal: bool = False):
        files = {'database': self.path}
        if wal:
            files['-wal'] = Path(str(self.path)+'-wal')
            files['-wal'].write_bytes(b'fixed backup WAL bytes')
        evidence: dict[str, Any] = {'verified': True, 'archive_sha256': 'a'*64,
            'archive_sha256_expected': 'a'*64, 'members': {},
            'tables': {self.path.name+':inputs': {'readable': True,
                'schema_matches_source': True, 'expected_rows': 3}}}
        for key, path in files.items():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            relative = self.path.name+('' if key == 'database' else key)
            evidence['members'][relative] = {'byte_identical': True, 'bytes': path.stat().st_size,
                'archive_sha256': digest, 'extracted_sha256': digest}
        target = self.root/'evidence.json'
        target.write_text(json.dumps(evidence))
        return target, evidence

    def test_metadata_only_ctime_resume_preserves_completed_copy_and_audits_hashes(self):
        db = _FakePG(self.targets)
        db.fail_after_verified = 1
        with self.assertRaises(ConnectionError):
            self.verify(db)
        evidence, _ = self.metadata_evidence()
        os.chmod(self.path,0o400)
        pinned = json.loads(next(iter(db.data['states'].values())))['source_signature']

        def changed_ctime(path, schema):
            signature = _source_signature(path, schema)
            # Fast filesystems may give chmod and file creation the same
            # timestamp. Model the metadata-only event explicitly so this
            # rejection/resume test never depends on filesystem clock ticks.
            signature['files']['database']['ctime_ns'] = max(
                signature['files']['database']['ctime_ns'],
                pinned['files']['database']['ctime_ns'] + 1)
            return signature

        with patch('tools.verify_sqlite_postgres._source_signature', side_effect=changed_ctime):
            with self.assertRaisesRegex(ValueError, '拒绝续验'):
                self.verify(_FakePG(self.targets,db.data))
            fresh = _FakePG(self.targets,db.data)
            result = self.verify(fresh,metadata_only_evidence=evidence)
        self.assertTrue(result['complete'])
        self.assertEqual(fresh.copy_rows,0)
        self.assertEqual(result['verified_rows'],3)
        audit = result['metadata_only_resume_audit'][0]
        self.assertNotEqual(audit['old_signature'],audit['new_signature'])
        self.assertEqual(audit['complete_sha256']['database'],hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(audit['evidence_sha256'],hashlib.sha256(evidence.read_bytes()).hexdigest())

    def test_metadata_only_hashes_database_and_wal_and_rejects_missing_member(self):
        evidence, manifest = self.metadata_evidence(wal=True)
        old = _source_signature(self.path,[])
        os.chmod(self.path,0o400)
        current = _source_signature(self.path,[])
        state = {'source_signature':old,'source_rows':3}
        with redirect_stdout(io.StringIO()):
            result = _reconcile_metadata(self.path,self.path.name,'inputs',[],state,current,evidence)
        self.assertEqual(set(result['metadata_only_resume_audit'][0]['complete_sha256']),{'database','-wal'})
        del manifest['members'][self.path.name+'-wal']
        evidence.write_text(json.dumps(manifest))
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError,'成员'):
            _reconcile_metadata(self.path,self.path.name,'inputs',[],state,current,evidence)

    def test_metadata_only_rejects_unverified_backup_wrong_hash_or_changed_mtime(self):
        evidence, manifest = self.metadata_evidence()
        old = _source_signature(self.path,[])
        os.chmod(self.path,0o400)
        current = _source_signature(self.path,[])
        state = {'source_signature':old,'source_rows':3}
        for changes in ({'verified':False},{'archive_sha256_expected':'b'*64}):
            evidence.write_text(json.dumps({**manifest,**changes}))
            with self.assertRaises(ValueError):
                _reconcile_metadata(self.path,self.path.name,'inputs',[],state,current,evidence)
        bad = copy.deepcopy(manifest)
        bad['members'][self.path.name].update(archive_sha256='b'*64,extracted_sha256='b'*64)
        evidence.write_text(json.dumps(bad))
        with self.assertRaisesRegex(ValueError,'SHA256'):
            _reconcile_metadata(self.path,self.path.name,'inputs',[],state,current,evidence)
        evidence.write_text(json.dumps(manifest))
        changed = copy.deepcopy(current)
        changed['files']['database']['mtime_ns'] += 1
        with self.assertRaisesRegex(ValueError,'只允许ctime'):
            _reconcile_metadata(self.path,self.path.name,'inputs',[],state,changed,evidence)

    def test_metadata_only_source_change_during_full_hash_never_updates_original_state(self):
        evidence, _ = self.metadata_evidence()
        old = _source_signature(self.path,[])
        os.chmod(self.path,0o400)
        current = _source_signature(self.path,[])
        state = {'source_signature':old,'source_rows':3}
        def changed_during_hash(stream, algorithm):
            digest = hashlib.sha256(stream.read())
            stat = self.path.stat()
            os.utime(self.path,ns=(stat.st_atime_ns,stat.st_mtime_ns+1))
            return digest
        with patch('tools.verify_sqlite_postgres.hashlib.file_digest',side_effect=changed_during_hash), \
                redirect_stdout(io.StringIO()),self.assertRaisesRegex(ValueError,'已变化'):
            _reconcile_metadata(self.path,self.path.name,'inputs',[],state,current,evidence)
        self.assertEqual(state,{'source_signature':old,'source_rows':3})

    def test_full_coverage_exact_hash_with_negative_and_gapped_rowids(self):
        db = _FakePG(self.targets)
        result = self.verify(db)
        self.assertTrue(result['complete'])
        self.assertEqual(result['verified_rows'], 3)
        self.assertEqual(result['copied_rows'], 3)
        self.assertEqual(result['missing_rows'], 0)
        self.assertEqual(result['content_mismatches'], 0)
        self.assertEqual(result['copy_after'], 7)
        self.assertEqual(result['verify_after'], 7)
        self.assertIn(result['stage'], db.data['stages'])
        self.assertTrue(any("SET work_mem='256MB'" in sql for sql, _ in db.calls))
        cleanup_resumable(db, result)
        self.assertFalse(db.data['stages'])
        self.assertFalse(db.data['states'])

    def test_failed_checkpoint_rolls_back_copy_rows_and_resume_copies_exactly_once(self):
        db = _FakePG(self.targets)
        db.fail_checkpoint = True
        with self.assertRaises(ConnectionError):
            self.verify(db)
        self.assertFalse(next(iter(db.data['stages'].values())))
        saved = json.loads(next(iter(db.data['states'].values())))
        self.assertEqual(saved['copied_rows'], 0)
        resumed = _FakePG(self.targets, db.data)
        result = self.verify(resumed)
        self.assertEqual(resumed.copy_rows, 3)
        self.assertEqual(result['verified_rows'], 3)

    def test_verification_disconnect_resumes_committed_stage_and_last_batch(self):
        db = _FakePG(self.targets)
        db.fail_after_verified = 1
        with self.assertRaises(ConnectionError):
            self.verify(db)
        saved = json.loads(next(iter(db.data['states'].values())))
        self.assertTrue(saved['copy_complete'])
        self.assertEqual(saved['verified_rows'], 2)
        resumed = _FakePG(self.targets, db.data)
        result = self.verify(resumed)
        self.assertEqual(resumed.copy_rows, 0)
        self.assertEqual(result['verified_rows'], 3)
        self.assertEqual(resumed.verify_calls, 2)  # remaining one + EOF

    def test_copy_disconnect_keeps_committed_prefix_and_resumes_source_rowid(self):
        db = _FakePG(self.targets)
        db.fail_copy_after = 2
        with self.assertRaises(ConnectionError):
            self.verify(db)
        state = json.loads(next(iter(db.data['states'].values())))
        self.assertEqual(state['copied_rows'], 2)
        self.assertEqual(state['copy_after'], 3)
        resumed = _FakePG(self.targets, db.data)
        result = self.verify(resumed)
        self.assertEqual(resumed.copy_rows, 1)
        self.assertEqual(result['verified_rows'], 3)

    def test_source_change_during_copy_rolls_back_rows_and_checkpoint(self):
        db = _FakePG(self.targets)
        with patch('tools.verify_sqlite_postgres._assert_source',
                   side_effect=[None, None, ValueError('source changed during COPY')]):
            with self.assertRaisesRegex(ValueError, 'during COPY'):
                self.verify(db)
        self.assertFalse(next(iter(db.data['stages'].values())))
        state = json.loads(next(iter(db.data['states'].values())))
        self.assertEqual(state['copied_rows'], 0)

    def test_changed_source_and_schema_refuse_resume_without_discarding_stage(self):
        for schema_change in (False, True):
            db = _FakePG(self.targets)
            db.fail_after_verified = 1
            with self.assertRaises(ConnectionError):
                self.verify(db, run_id=str(schema_change))
            before = copy.deepcopy(db.data)
            with closing(sqlite3.connect(self.path)) as source, source:
                if schema_change:
                    source.execute('ALTER TABLE inputs ADD COLUMN extra TEXT')
                else:
                    source.execute("UPDATE inputs SET x='changed' WHERE match_id='m1'")
            resumed = _FakePG(self.targets, db.data)
            with self.assertRaisesRegex(ValueError, '源身份'):
                self.verify(resumed, run_id=str(schema_change))
            self.assertEqual(db.data, before)
            self.assertEqual(resumed.copy_rows, 0)

    def test_full_hash_mismatch_and_missing_target_never_mark_complete_or_cleanup(self):
        for targets in ({**self.targets, tuple(self.rows[0][:6]): b'wrong'},
                        {key: value for key, value in self.targets.items() if key != tuple(self.rows[1][:6])}):
            db = _FakePG(targets)
            with self.assertRaisesRegex(ValueError, '内容核验失败'):
                self.verify(db)
            state = json.loads(next(iter(db.data['states'].values())))
            self.assertFalse(state['complete'])
            self.assertEqual(state['verified_rows'], 3)
            self.assertTrue(state['missing_rows'] or state['content_mismatches'])
            with self.assertRaises(ValueError):
                cleanup_resumable(db, {'resumable': True, 'complete': False})
            self.assertTrue(db.data['stages'])

    def test_cancelled_statement_retries_boundedly_on_same_stage(self):
        db = _FakePG(self.targets)
        db.timeout_queries = 2
        with patch('tools.verify_sqlite_postgres.time.sleep'):
            result = self.verify(db, retries=2)
        self.assertTrue(result['complete'])
        db = _FakePG(self.targets)
        db.timeout_queries = 3
        with patch('tools.verify_sqlite_postgres.time.sleep'), self.assertRaises(_StatementTimeout):
            self.verify(db, retries=2)
        self.assertEqual(db.verify_calls, 3)
        self.assertTrue(db.data['stages'])

    def test_explain_rejects_full_target_scan_before_verification(self):
        db = _FakePG(self.targets)
        db.plan = [{'Plan': {'Node Type': 'Seq Scan', 'Relation Name': 'model_inputs'}}]
        with self.assertRaisesRegex(ValueError, '主键索引'):
            self.verify(db)
        self.assertEqual(db.verify_calls, 0)
        self.assertTrue(db.data['stages'])

    def test_explain_rejects_incomplete_key_lookup(self):
        db = _FakePG(self.targets)
        db.plan[0]['Plan']['Plans'][0]['Index Cond'] = 'match_id=s.match_id'
        with self.assertRaisesRegex(ValueError, '主键索引'):
            self.verify(db)
        self.assertEqual(db.verify_calls, 0)

    def test_null_primary_key_is_not_silently_stringified(self):
        with closing(sqlite3.connect(self.path)) as source, source:
            source.execute('UPDATE inputs SET outcome=NULL WHERE match_id=?', ('m1',))
        db = _FakePG(self.targets)
        with self.assertRaisesRegex(ValueError, '主键存在NULL'):
            self.verify(db)
        self.assertEqual(db.copy_rows, 0)

    def test_postgres_recovery_missing_stage_tail_refuses_checkpoint_skip(self):
        db = _FakePG(self.targets)
        db.fail_after_verified = 1
        with self.assertRaises(ConnectionError):
            self.verify(db)
        next(iter(db.data['stages'].values())).clear()
        with self.assertRaisesRegex(ValueError, '丢失已提交数据'):
            self.verify(db)

    def test_empty_source_verified_as_zero_and_bad_options_fail_before_pg(self):
        with closing(sqlite3.connect(self.path)) as source, source:
            source.execute('DELETE FROM inputs')
        db = _FakePG({})
        result = self.verify(db)
        self.assertEqual(result['verified_rows'], 0)
        self.assertTrue(result['complete'])
        with self.assertRaises(ValueError):
            verify_resumable(db, self.root, self.path.name, 'inputs', 'model_inputs', verify_batch=0)

    def test_run_identity_is_stable_and_source_stat_detects_change(self):
        stage = _stage_names(self.path, 'model_inputs', None)
        signature = _source_signature(self.path, ['schema'])
        with self.path.open('ab') as source:
            source.write(b'change')
        self.assertEqual(_stage_names(self.path, 'model_inputs', None), stage)
        self.assertNotEqual(_source_signature(self.path, ['schema']), signature)

    def test_report_is_persisted_atomically_and_failed_replace_preserves_previous_report(self):
        path = self.root/'report.json'
        _write_report(path, {'verified': False})
        self.assertFalse(json.loads(path.read_text())['verified'])
        with patch('tools.verify_sqlite_postgres.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                _write_report(path, {'verified': True})
        self.assertFalse(json.loads(path.read_text())['verified'])

    def test_bounded_sql_hashes_every_field_inside_index_lookup_and_preserves_source_rows(self):
        sql = _bounded_verify_sql('stage', 'model_inputs', ('match_id','at'), ('match_id','at','x'), ('TEXT',)*3)
        self.assertIn('WITH batch AS MATERIALIZED', sql)
        self.assertIn('LEFT JOIN LATERAL', sql)
        self.assertIn('OFFSET 0', sql)
        self.assertIn('t."x"', sql)
        self.assertIn('source_sha', sql)
        self.assertNotIn("'hex') AS target_sha", sql)
        with self.assertRaises(ValueError):
            _require_target_index([], 'model_inputs')


class MigrationFingerprintTests(unittest.TestCase):
    def test_values_are_framed_and_float_bits_are_preserved(self):
        self.assertNotEqual(fingerprint(['ab', 'c'], ['TEXT', 'TEXT']),
                            fingerprint(['a', 'bc'], ['TEXT', 'TEXT']))
        self.assertNotEqual(fingerprint([None], ['TEXT']), fingerprint([''], ['TEXT']))
        self.assertEqual(fingerprint([1], ['REAL']), fingerprint([1.0], ['REAL']))
        self.assertNotEqual(fingerprint([1.0], ['REAL']), fingerprint([1.0000001], ['REAL']))
        self.assertEqual(len(fingerprint(['中文', 1, .5], ['TEXT', 'INTEGER', 'REAL'])), 64)

    def test_sql_uses_binary_float_and_utf8_framing(self):
        sql = fingerprint_sql(['x', 'at', 'cutoff_ms'], ['TEXT', 'INTEGER', 'REAL'])
        self.assertIn('sha256', sql)
        self.assertIn('float8send', sql)
        self.assertIn('octet_length', sql)
        with self.assertRaises(ValueError):
            fingerprint_sql(['bad;DROP TABLE'], ['TEXT'])


if __name__ == '__main__':
    unittest.main()
