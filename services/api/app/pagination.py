"""Signed keyset cursors scoped to the exact query, without copying its text."""

import base64
import binascii
from datetime import datetime
import hashlib
import hmac
import json

from fastapi import HTTPException


def _scope(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def encode_cursor(key: bytes, scope, when: datetime, identifier: str) -> str:
    if len(key) < 32 or when.tzinfo is None:
        raise ValueError('cursor signing configuration invalid')
    payload = json.dumps([1, _scope(scope), when.isoformat(), identifier], separators=(',', ':')).encode()
    signature = hmac.new(key, payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + signature).decode().rstrip('=')


def decode_cursor(key: bytes, scope, value: str | None) -> tuple[datetime, str] | None:
    if not value:
        return None
    try:
        if len(key) < 32 or len(value) > 1000:
            raise ValueError
        raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
        payload, signature = raw[:-32], raw[-32:]
        if not hmac.compare_digest(signature, hmac.new(key, payload, hashlib.sha256).digest()):
            raise ValueError
        version, scope_hash, stamp, identifier = json.loads(payload)
        if version != 1 or scope_hash != _scope(scope):
            raise ValueError
        when = datetime.fromisoformat(stamp)
        if when.tzinfo is None or not isinstance(identifier, str) or not identifier or len(identifier) > 160:
            raise ValueError
        return when, identifier
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise HTTPException(400, '分页游标无效或查询条件已变化') from None
