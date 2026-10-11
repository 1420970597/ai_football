import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from store.ledger_db import LedgerDB
from store.training_data import snapshot_inputs, write_inputs
from tests.test_training_data import decision
from tools.repair_model_databases import repair


class RepairModelDatabaseTests(unittest.TestCase):
    def test_old_vectors_are_rebuilt_or_explicitly_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority,target = root/'clean.sqlite3',root/'old.sqlite3'
            rows = snapshot_inputs(decision())
            write_inputs(authority,rows)
            write_inputs(target,[rows[0],{**rows[0],'match_id':'missing'}])
            with sqlite3.connect(target) as con:
                con.execute("UPDATE inputs SET data_version=NULL,x='[999]'")
            result = repair(target,authority,{})
            self.assertEqual(result['states']['rebuilt_verified_snapshot'],1)
            self.assertEqual(result['states']['missing_frozen_input_snapshot'],1)
            with sqlite3.connect(target) as con:
                self.assertEqual(con.execute("SELECT x FROM inputs WHERE match_id='m1'").fetchone()[0],rows[0]['x'])
                self.assertIsNone(con.execute("SELECT x FROM inputs WHERE match_id='missing'").fetchone()[0])
            self.assertEqual(repair(target,authority,{}),result)

    def test_retired_prediction_preserves_probability_and_uses_correct_features(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority,target = root/'clean.sqlite3',root/'shadow.sqlite3'
            row = snapshot_inputs(decision())[0]
            write_inputs(authority,[row])
            with sqlite3.connect(target) as con:
                con.execute('CREATE TABLE predictions(version TEXT,match_id TEXT,at TEXT,cutoff_ms REAL,algorithm TEXT,'
                            'market TEXT,line TEXT,outcome TEXT,baseline REAL,x TEXT,probability REAL)')
                con.execute('INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                            ['old',*[row[k] for k in ('match_id','at','cutoff_ms','algorithm','market','line','outcome','baseline')], '[999]',.9])
            repair(target,authority,{})
            with sqlite3.connect(target) as con:
                self.assertEqual(con.execute('SELECT original_probability FROM predictions').fetchone()[0],.9)
                self.assertIsNone(con.execute('SELECT probability FROM predictions').fetchone()[0])
                self.assertEqual(con.execute('SELECT x FROM predictions').fetchone()[0],row['x'])

    def test_ledger_backup_regrades_negative_handicap_and_keeps_time(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'ledger.sqlite3'
            db = LedgerDB(path)
            row = {'match_id':'m','at':'2025-01-01T12:00:00+00:00','decision_id':'x',
                   'market':'AH','line':'-0/0.5','outcome':'home','is_live':True,'entry_score':[1,0],
                   'settlement_basis':'full_score','model_version':'live-poisson-market-v1',
                   'status':'won','odds':1.9,'is_pick':True,'pnl':.9,'competition_type':'real',
                   'ft_score':[1,0],'settled_at':'2025-01-01T14:00:00+00:00'}
            db.put([row],lambda _:1.0)
            db.connection.commit()
            db.connection.close()
            repair(path,Path(directory)/'unused',{'m':{'ft_score':[1,0]}})
            with sqlite3.connect(path) as con:
                out = json.loads(con.execute('SELECT payload FROM decisions').fetchone()[0])
            self.assertEqual(out['status'],'half_lost')
            self.assertEqual(out['pnl'],-.5)
            self.assertEqual(out['settled_at'],row['settled_at'])
