"""Authoritative versioned runtime settings in PostgreSQL."""
from __future__ import annotations

from contextlib import closing
import json
from typing import Any, Mapping

from store.postgres import connect

SETTINGS_KEY = 'runtime'
SCHEMA = ('CREATE TABLE IF NOT EXISTS runtime_settings '
          '(name TEXT PRIMARY KEY,version BIGINT NOT NULL CHECK(version>=1),'
          'settings JSONB NOT NULL,updated_at TIMESTAMPTZ NOT NULL DEFAULT now())')


def load_runtime_settings(seed: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """First initializer wins; an existing PG document always takes precedence.

    Reader workers never create schema. Initialization is explicit and confined
    to the API startup, outside request/decision paths.
    """
    with closing(connect()) as db:
        if seed is not None:
            with db:
                db.execute(SCHEMA).close()
                db.execute('INSERT INTO runtime_settings(name,version,settings) VALUES (?,?,?::jsonb) '
                           'ON CONFLICT(name) DO NOTHING',
                           (SETTINGS_KEY, seed['version'], json.dumps(seed['settings'],
                               ensure_ascii=False, allow_nan=False))).close()
        row = db.execute('SELECT version,settings FROM runtime_settings WHERE name=?',
                         (SETTINGS_KEY,)).fetchone()
        if row is None:
            return None
        settings = json.loads(row[1]) if isinstance(row[1], str) else row[1]
        if not isinstance(settings, dict):
            raise ValueError('PG 配置文档无效')
        return {'version': int(row[0]), 'settings': settings}


def save_runtime_settings(document: Mapping[str, Any], expected_version: int) -> bool:
    """Atomically reject another instance's edit; commit before local apply."""
    with closing(connect()) as db, db:
        result = db.execute('UPDATE runtime_settings SET version=?,settings=?::jsonb,updated_at=now() '
                            'WHERE name=? AND version=?',
                            (document['version'], json.dumps(document['settings'],ensure_ascii=False,
                                allow_nan=False), SETTINGS_KEY, expected_version))
        changed = result.rowcount
        result.close()
        return changed == 1
