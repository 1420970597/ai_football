import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.migrate_sqlite_to_postgres import _ident, main


class MigrateStorageTests(unittest.TestCase):
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
