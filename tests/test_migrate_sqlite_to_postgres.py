import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.migrate_sqlite_to_postgres import _create_target, _ident, main
from unittest.mock import MagicMock


class MigrateStorageTests(unittest.TestCase):
    def test_fresh_and_existing_schema_include_prediction_receipt_clocks(self):
        db = MagicMock()
        _create_target(db)
        statements = [str(call.args[0]) for call in db.execute.call_args_list]
        create = next(sql for sql in statements if 'CREATE TABLE IF NOT EXISTS model_predictions(' in sql)
        for name in ('decision_received_at_ms', 'quote_received_at_ms', 'odds'):
            self.assertIn(name, create)
            self.assertTrue(any('ALTER TABLE model_predictions ADD COLUMN IF NOT EXISTS '+name in sql
                                for sql in statements))
        db.commit.assert_called_once()

    def test_identifier_rejects_sql_injection(self):
        self.assertEqual(_ident("model_inputs"), '"model_inputs"')
        with self.assertRaises(ValueError):
            _ident("inputs;DROP TABLE orders")

    def test_missing_pg_credentials_fails_before_source_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "ledger.sqlite3"
            source.write_bytes(b"unchanged")
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(main(["--root", directory, "--env-file", str(Path(directory) / "missing.env")]), 2)
            self.assertEqual(source.read_bytes(), b"unchanged")


if __name__ == "__main__":
    unittest.main()
