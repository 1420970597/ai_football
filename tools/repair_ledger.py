#!/usr/bin/env python3
"""Regrade an offline ledger copy using explicitly confirmed local final scores.

Run on a copy first. For production, stop the service and back up the ledger
before invoking the same command. Does not invent final scores or old identities.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.live_model import valid_score
from service.ledger import DecisionLedger, LedgerEntry


def repair(root: Path, score_path: Path) -> dict:
    ledger = DecisionLedger(root)
    raw = json.loads(score_path.read_text(encoding='utf-8'))
    scores = raw.get('scores', {})
    confirmed = {str(mid): row for mid, row in scores.items()
                 if isinstance(row, dict) and row.get('done') is True and valid_score(row.get('ft')) is not None}
    before = ledger.stats()
    migrated = unverified = 0
    # Only the audited live generation has proven erroneous AH metadata.
    # Keep original input probabilities/version and append a correction revision.
    if ledger._db:
        cursor = 0
        while True:
            batch = ledger._db.connection.execute("SELECT rowid,payload FROM decisions WHERE rowid>? AND is_live=1 "
                "AND model_version='live-poisson-market-v1' AND market IN ('AH','AH_1H') ORDER BY rowid LIMIT 256", (cursor,)).fetchall()
            if not batch:
                break
            pending = []
            for rid, payload in batch:
                cursor = rid
                entry = LedgerEntry.from_dict(json.loads(payload))
                if entry.settlement_basis == 'remaining_score':
                    continue
                if valid_score(entry.entry_score) is None:
                    unverified += 1
                    continue
                entry.settlement_basis = 'remaining_score'
                entry.updated_at = datetime.now(timezone.utc).isoformat()
                pending.append(entry)
                migrated += 1
            ledger._append(pending)
    repair_result = {'scanned': 0, 'settled': 0, 'void': 0, 'skipped': 0}
    items = list(confirmed.items())
    for offset in range(0, len(items), 50):
        result = ledger.settle(dict(items[offset:offset+50]), regrade=True)
        for key in ('settled', 'void'):
            repair_result[key] += result[key]
    repair_result['scanned'] = after_total = ledger.stats(only_picks=False)['total']
    repair_result['skipped'] = after_total - repair_result['settled'] - repair_result['void']
    after = ledger.stats()
    return {'before': before, 'regrade': repair_result, 'after': after,
            'migrated_live_handicap_basis': migrated, 'unverified_entry_scores': unverified,
            'confirmed_matches': len(confirmed), 'unconfirmed_scores': len(scores) - len(confirmed),
            'limitation': 'Legacy decisions collapsed by old key cannot be reconstructed; pending remains without final proof.'}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--ledger', type=Path, required=True)
    parser.add_argument('--scores', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    result = repair(args.ledger, args.scores)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k: result[k] for k in ('regrade', 'confirmed_matches', 'unconfirmed_scores')}, ensure_ascii=False))
    print('picks: before graded=%d, after graded=%d, pending=%d' %
          (result['before']['graded'], result['after']['graded'], result['after']['pending']))


if __name__ == '__main__':
    main()
