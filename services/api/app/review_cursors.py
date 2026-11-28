"""Short signed search positions, with read compatibility for existing cursors."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid

PREFIX = 'c2.'
MAX_POSITION = (1 << 63) - 1


def _scope(review_id, attempt_id, search_key: str) -> bytes:
    return (b'review-search-cursor-v2\0' + uuid.UUID(str(review_id)).bytes
            + uuid.UUID(str(attempt_id)).bytes + bytes.fromhex(search_key))


def encode_cursor(key: bytes, review_id, attempt_id, search_key: str, position: int) -> str:
    if type(position) is not int or not 1 <= position <= MAX_POSITION:
        raise ValueError('invalid search position')
    payload = position.to_bytes(8, 'big')
    signature = hmac.new(key, _scope(review_id, attempt_id, search_key) + payload, hashlib.sha256).digest()
    return PREFIX + base64.urlsafe_b64encode(payload + signature).decode().rstrip('=')


def decode_cursor(key: bytes, review_id, attempt_id, search_key: str, cursor: str) -> int:
    if cursor.startswith(PREFIX):
        token = cursor[len(PREFIX):]
        raw = base64.b64decode(token + '=' * (-len(token) % 4), altchars=b'-_', validate=True)
        if len(raw) != 40 or base64.urlsafe_b64encode(raw).decode().rstrip('=') != token:
            raise ValueError('invalid cursor encoding')
        payload, signature = raw[:8], raw[8:]
        expected = hmac.new(key, _scope(review_id, attempt_id, search_key) + payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError('invalid cursor signature')
        position = int.from_bytes(payload, 'big')
    else:
        # Existing in-flight searches and their stored evidence remain usable.
        raw = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
        payload, signature = raw[:-32], raw[-32:]
        if not hmac.compare_digest(signature, hmac.new(key, payload, hashlib.sha256).digest()):
            raise ValueError('invalid cursor signature')
        old_review, old_attempt, old_search, position = json.loads(payload)
        if (old_review, old_attempt, old_search) != (str(review_id), str(attempt_id), search_key):
            raise ValueError('cross-query cursor')
    if type(position) is not int or not 1 <= position <= MAX_POSITION:
        raise ValueError('invalid search position')
    return position
