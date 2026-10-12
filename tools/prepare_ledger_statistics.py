#!/usr/bin/env python3
"""Prepare PostgreSQL history statistics without blocking an API request."""
from __future__ import annotations

import json
import time

from store.ledger_db import prepare_postgres_statistics


def main() -> None:
    started = time.monotonic()
    result = prepare_postgres_statistics()
    result['elapsed_ms'] = round((time.monotonic() - started) * 1000, 3)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
