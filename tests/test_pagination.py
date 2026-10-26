from datetime import datetime, timezone
import unittest

from fastapi import HTTPException

from app.pagination import decode_cursor, encode_cursor


class PaginationTests(unittest.TestCase):
    def test_scope_is_compact_and_roundtrip_preserves_boundary(self):
        now = datetime.now(timezone.utc)
        scope = {'kind': 'fixtures', 'leagues': ['x' * 100] * 100}
        value = encode_cursor(b'k' * 32, scope, now, 'fixture-id')
        self.assertLess(len(value), 1000)
        self.assertEqual((now, 'fixture-id'), decode_cursor(b'k' * 32, scope, value))

    def test_tamper_other_scope_other_key_and_malformed_input_rejected(self):
        value = encode_cursor(b'k' * 32, {'author': 1}, datetime.now(timezone.utc), 'message')
        for key, scope, cursor in [(b'z' * 32, {'author': 1}, value),
                                  (b'k' * 32, {'author': 2}, value),
                                  (b'k' * 32, {'author': 1}, value[:-8] + 'AAAAAAAA'),
                                  (b'k' * 32, {'author': 1}, 'not%%%base64')]:
            with self.assertRaises(HTTPException):
                decode_cursor(key, scope, cursor)
