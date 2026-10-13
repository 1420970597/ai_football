#!/usr/bin/env python3
"""Reproduce the audit's ROI evidence from the production PostgreSQL ledger.

Needs PG_* environment variables (from the repo .env). Writes
data/roi_evidence.json and prints a summary. Bootstrap resamples matches, not
rows, because rows inside one match are strongly correlated.
"""
import json, os, random, sys
from collections import defaultdict
import psycopg

OUT = os.path.join(os.path.dirname(__file__), '..', 'data')
GRADED = ('won', 'lost', 'half_won', 'half_lost', 'push')


def boot(groups, reps=2000, seed=20261013):
    keys = list(groups)
    rng = random.Random(seed)
    stake = sum(groups[k][0] for k in keys)
    pnl = sum(groups[k][1] for k in keys)
    draws = []
    for _ in range(reps):
        s = p = 0.0
        for _ in keys:
            a, b = groups[rng.choice(keys)]
            s += a; p += b
        draws.append(p / s)
    draws.sort()
    return {'matches': len(keys), 'stake': round(stake, 2), 'pnl': round(pnl, 2),
            'roi': round(pnl / stake, 4),
            'ci95': [round(draws[int(.025 * reps)], 4), round(draws[int(.975 * reps)], 4)]}


def main():
    conn = psycopg.connect(host=os.environ['PG_BASE_URL'], port=int(os.environ['PG_BASE_PORT']),
                           dbname=os.environ['PG_DATASET'], user=os.environ['PG_USER'],
                           password=os.environ['PG_PASSWORD'], connect_timeout=15)
    cur = conn.cursor()
    cur.execute("""select match_id, league, market, odds, p_fused, p_market, pnl from decision_statistics
        where decision='recommendation' and algorithm='economic_ensemble' and competition_type='real'
        and status = any(%s)""", (list(GRADED),))
    rows = cur.fetchall()
    scen = {
        'sim_all_real_labelled': lambda r: True,
        'sim_exclude_8min_virtual': lambda r: '分钟' not in r[1],
        'sim_gap_le_20pp_non_virtual': lambda r: '分钟' not in r[1] and (r[4] - r[5]) <= .20,
        'sim_gap_le_10pp_non_virtual': lambda r: '分钟' not in r[1] and (r[4] - r[5]) <= .10,
        'sim_gap_gt_30pp_non_virtual': lambda r: '分钟' not in r[1] and (r[4] - r[5]) > .30,
    }
    result = {}
    for name, keep in scen.items():
        g = defaultdict(lambda: [0.0, 0.0])
        for r in rows:
            if keep(r):
                g[r[0]][0] += 1; g[r[0]][1] += float(r[6])
        result[name] = boot(g)
    # Real orders, priced at the decision odds of the matched recommendation.
    import csv
    g = defaultdict(lambda: [0.0, 0.0]); gv = defaultdict(lambda: [0.0, 0.0])
    with open(os.path.join(OUT, 'real_orders_joined.csv')) as f:
        for r in csv.DictReader(f):
            if r['sim_status'] == 'pending':
                continue
            s = float(r['stake']); p = s * float(r['sim_pnl_per_unit'])
            g[r['match_id']][0] += s; g[r['match_id']][1] += p
            if '分钟' not in r['league'] and float(r['p_fused']) - 1 / float(r['decision_odds']) <= .20:
                gv[r['match_id']][0] += s; gv[r['match_id']][1] += p
    result['real_orders_all_graded'] = boot(g)
    result['real_orders_gap_le_20pp_non_virtual'] = boot(gv)
    with open(os.path.join(OUT, 'roi_evidence.json'), 'w') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    for k, v in result.items():
        print('%-38s %s' % (k, v))


if __name__ == '__main__':
    sys.exit(main())
