#!/usr/bin/env python3
"""Regrade an offline ledger copy using explicitly confirmed local final scores.

Run on a copy first. For production, stop the service and back up the ledger
before invoking the same command. Does not invent final scores or old identities.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.live_model import valid_score
from service.ledger import DecisionLedger


def repair(root: Path, score_path: Path) -> dict:
    ledger = DecisionLedger(root)
    raw = json.loads(score_path.read_text(encoding='utf-8'))
    scores = raw.get('scores', {})
    confirmed = {str(mid): row for mid, row in scores.items()
                 if isinstance(row, dict) and row.get('done') is True and valid_score(row.get('ft')) is not None}
    before = ledger.stats()
    repair_result = ledger.settle(confirmed, regrade=True)
    after = ledger.stats()
    return {'before': before, 'regrade': repair_result, 'after': after,
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
