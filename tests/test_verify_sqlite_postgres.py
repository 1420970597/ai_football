import unittest

from tools.verify_sqlite_postgres import fingerprint, fingerprint_sql


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
