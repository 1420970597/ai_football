#!/usr/bin/env python3
"""Measure how many ensemble picks are published shortly after a score change.

Reads one hourly live-replay journal (gzip JSONL). For each match it tracks the
last time the score changed, then records every published pick together with
the seconds since that change and the model-minus-market probability gap.
Output: CSV rows per (match, market, line, outcome) first appearance. Match IDs
are pseudonymized; absolute publish time is omitted, relative timing retained.
The input journal fixes the observation scope; this does not audit the full DB.
"""
import csv
import gzip
import json
import sys
from pathlib import Path

from archive_privacy import POST_GOAL_FIELDS, redact_post_goal_pick


def main():
    src, dst = sys.argv[1], Path(sys.argv[2]).resolve()
    project = Path(__file__).resolve().parents[3]
    archive = Path(__file__).resolve().parents[1] / 'data'
    if not dst.is_relative_to(project) or dst.is_relative_to(archive):
        raise ValueError('output must stay inside this project and outside fixed archive data')
    dst.parent.mkdir(parents=True, exist_ok=True)
    last_score, changed_at, seen = {}, {}, set()
    with gzip.open(src, 'rt', encoding='utf-8') as f, dst.open('w', newline='', encoding='utf-8') as out:
        w = csv.DictWriter(out, fieldnames=POST_GOAL_FIELDS, lineterminator='\n')
        w.writeheader()
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            res = d.get('result') or d
            snap = d.get('snapshot') or {}
            mid = str(d.get('match_id') or res.get('match_id') or '')
            cap = d.get('published_at_ms') or 0
            sc = tuple(snap.get('score') or res.get('score') or ())
            if mid not in last_score or last_score[mid] != sc:
                if mid in last_score:
                    changed_at[mid] = cap
                last_score[mid] = sc
            for p in res.get('picks') or []:
                key = (mid, p.get('market'), str(p.get('line')), p.get('outcome'))
                if key in seen:
                    continue
                seen.add(key)
                since = (cap - changed_at[mid]) / 1000 if mid in changed_at else ''
                w.writerow(redact_post_goal_pick(dict(
                    match_id=mid, clock=res.get('clock'), score=list(sc),
                    secs_since_score_change=since, market=p.get('market'),
                    line=p.get('line'), outcome=p.get('outcome'), odds=p.get('odds'),
                    p_model=p.get('p_model'), p_market=p.get('p_market'))))


if __name__ == '__main__':
    main()
