import base64
import hashlib
import hmac
import json
import unittest
import uuid

from app.review_cursors import encode_cursor, decode_cursor, MAX_POSITION


class ReviewCursorTests(unittest.TestCase):
    def setUp(self):
        self.key = b'isolated-cursor-test-only'
        self.review, self.attempt = uuid.uuid4(), uuid.uuid4()
        self.search = 'a' * 64

    def encode(self, position=100):
        return encode_cursor(self.key, self.review, self.attempt, self.search, position)

    def decode(self, token, **changes):
        values = dict(key=self.key, review_id=self.review, attempt_id=self.attempt, search_key=self.search)
        return decode_cursor(**(values | changes), cursor=token)

    def legacy(self, position=100):
        payload = json.dumps([str(self.review), str(self.attempt), self.search, position], separators=(',', ':')).encode()
        return base64.urlsafe_b64encode(payload + hmac.new(self.key, payload, hashlib.sha256).digest()).decode().rstrip('=')

    def test_short_cursor_roundtrip_and_entire_position_range(self):
        for position in (1, 100, 999999, MAX_POSITION):
            token = self.encode(position)
            self.assertEqual(57, len(token))
            self.assertEqual(position, self.decode(token))
            self.assertLess(len(token), len(self.legacy(position)))

    def test_both_formats_bind_key_job_attempt_and_query(self):
        for token in (self.encode(), self.legacy()):
            for change in ({'key': b'other-test-key'}, {'review_id': uuid.uuid4()},
                           {'attempt_id': uuid.uuid4()}, {'search_key': 'b' * 64}):
                with self.subTest(change=list(change), short=token.startswith('c2.')), self.assertRaises(ValueError):
                    self.decode(token, **change)

    def test_every_single_byte_mutation_and_truncation_is_rejected(self):
        token = self.encode()
        raw = base64.urlsafe_b64decode(token[3:] + '==')
        self.assertEqual(40, len(raw))
        for i in range(len(raw)):
            changed = bytearray(raw)
            changed[i] ^= 1
            damaged = 'c2.' + base64.urlsafe_b64encode(changed).decode().rstrip('=')
            with self.subTest(byte=i), self.assertRaises(ValueError):
                self.decode(damaged)
        for damaged in ('c2.', token[:-1], token + '=', token + 'x', 'c2.' + '!' * 54):
            with self.subTest(damaged=damaged[:5]), self.assertRaises(ValueError):
                self.decode(damaged)

    def test_noncanonical_padding_bits_do_not_create_a_second_token(self):
        token = self.encode()
        alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
        last = alphabet.index(token[-1])
        damaged = token[:-1] + alphabet[last | 1]
        self.assertNotEqual(token, damaged)
        with self.assertRaises(ValueError):
            self.decode(damaged)

    def test_invalid_positions_are_not_emitted_or_accepted_in_legacy_format(self):
        for position in (0, -1, True, 1.5, MAX_POSITION + 1):
            with self.subTest(position=position), self.assertRaises(ValueError):
                self.encode(position)
            with self.subTest(legacy=position), self.assertRaises(ValueError):
                self.decode(self.legacy(position))

    def test_existing_valid_legacy_cursor_survives_upgrade(self):
        token = self.legacy()
        self.assertEqual(100, self.decode(token))
        self.assertNotEqual(token, self.encode())


if __name__ == '__main__':
    unittest.main()
