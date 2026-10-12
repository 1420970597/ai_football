#!/usr/bin/env python3
"""Backfill late-observed result labels without backdating their availability."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from store.ledger_db import LedgerDB


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--ledger', type=Path, required=True)
    parser.add_argument('--available-at', default='',
                        help='UTC ISO-8601 observation time; defaults to now')
    args = parser.parse_args()
    available_at = args.available_at or datetime.now(timezone.utc).isoformat()
    db = LedgerDB(args.ledger.resolve())
    try:
        result = db.backfill_observed_labels(available_at)
    finally:
        db.connection.close()
    print(json.dumps({'available_at': available_at, **result}, ensure_ascii=False))


if __name__ == '__main__':
    main()
