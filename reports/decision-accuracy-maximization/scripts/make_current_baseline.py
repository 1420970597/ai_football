#!/usr/bin/env python3
"""Rebuild a diagnostic market baseline from the current local model stores.

This deliberately labels the result diagnostic: the current input schema has
no first-received timestamp for every feature or label_available_at, so these
numbers are not a leakage-certified promotion baseline.
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.model_validation import evaluate
from store.training_data import dataset

LEDGER = ROOT / 'output/ledger/ledger.sqlite3'
INPUTS = ROOT / 'output/models/inputs.sqlite3'
OUTPUT = ROOT / 'reports/decision-accuracy-maximization/data/current_baseline.json'
MAX_ROWS_PER_MATCH = 64


def main() -> None:
    quality: dict[str, object] = {}
    rows = dataset(LEDGER, INPUTS, audit=quality, max_per_match=MAX_ROWS_PER_MATCH)
    result = evaluate(rows, [float(row['baseline']) for row in rows]) if rows else {}
    payload = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'classification': 'diagnostic_only_not_leakage_certified',
        'reason': ('Current input schema lacks per-feature received_at and append-only '
                   'label_available_at; settled_at is not a substitute.'),
        'source': {'ledger': str(LEDGER.relative_to(ROOT)),
                   'inputs': str(INPUTS.relative_to(ROOT))},
        'sampling': {'method': 'deterministic identity min-hash',
                     'max_rows_per_match': MAX_ROWS_PER_MATCH,
                     'note': 'This caps diagnostic evaluation rows per match; it does not describe archive retention.'},
        'data_quality': quality,
        'market_baseline': result,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(OUTPUT), 'rows': len(rows),
                      'matches': result.get('matches', 0),
                      'classification': payload['classification']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
