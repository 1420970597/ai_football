"""Short-lived, session-bound media grants; no S3 key leaves the API."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import time

from fastapi import HTTPException

from .pagination import decode_cursor, encode_cursor

MEDIA_GRANT_SECONDS = 60


def _scope(actor, message, index, media, context):
    return {'kind': 'private_media_v1', 'context': context, 'actor': str(actor['id']),
            'session': actor['_token_hash'], 'message': str(message['id']),
            'revision': message.get('revision', 1), 'index': index,
            'object': hashlib.sha256(str(media.get('object_key', '')).encode()).hexdigest(),
            'sha256': media.get('sha256')}


def issue(key, actor, message, index, media, context, *, now=None):
    issued = time.time() if now is None else now
    return encode_cursor(key, _scope(actor, message, index, media, context),
                         datetime.fromtimestamp(issued, timezone.utc), 'media')


def verify(key, actor, message, index, media, context, grant, *, now=None):
    try:
        decoded = decode_cursor(key, _scope(actor, message, index, media, context), grant)
        if decoded is None or decoded[1] != 'media':
            raise ValueError()
        age = (time.time() if now is None else now) - decoded[0].timestamp()
        if not 0 <= age < MEDIA_GRANT_SECONDS:
            raise ValueError()
    except (HTTPException, KeyError, TypeError, ValueError):
        raise HTTPException(403, '图片授权无效或已过期，请重新打开消息',
                            headers={'Cache-Control': 'private, no-store'}) from None
