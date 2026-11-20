"""Recover omitted continuation filters without widening a signed search."""
from __future__ import annotations

import hashlib
import json


def scope_key(scope: dict) -> str:
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()


def continuation_scope(requested: dict, recorded: object, search_key: str) -> dict:
    if isinstance(recorded, str):
        try:
            recorded = json.loads(recorded)
        except (ValueError, TypeError):
            raise ValueError('original search scope unavailable') from None
    if not isinstance(recorded, dict) or set(recorded) != set(requested):
        raise ValueError('original search scope unavailable')
    if scope_key(recorded) != search_key:
        raise ValueError('original search scope checksum mismatch')
    for field, value in requested.items():
        if field in {'team', 'phase'} or value not in (None, ''):
            if value != recorded[field]:
                raise ValueError('continuation filters conflict with original search')
    return recorded
