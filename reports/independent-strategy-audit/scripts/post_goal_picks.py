#!/usr/bin/env python3
"""Measure how many ensemble picks are published shortly after a score change.

Reads one hourly live-replay journal (gzip JSONL). For each match it tracks the
last time the score changed, then records every published pick together with
the seconds since that change and the model-minus-market probability gap.
Output: CSV rows per (match, market, line, outcome) first appearance.
"""
import csv, gzip, json, sys

src, dst = sys.argv[1], sys.argv[2]
last_score, changed_at, seen = {}, {}, set()
with gzip.open(src, 'rt') as f, open(dst, 'w', newline='') as out:
    w = csv.writer(out)
    w.writerow(['match_id', 'published_at_ms', 'clock', 'score', 'secs_since_score_change',
                'market', 'line', 'outcome', 'odds', 'p_model', 'p_market'])
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
            w.writerow([mid, cap, res.get('clock'), list(sc), since, p.get('market'), p.get('line'),
                        p.get('outcome'), p.get('odds'), p.get('p_model'), p.get('p_market')])
