#!/usr/bin/env python3
"""Pinned StatsBomb historical replay, train 2018 / hold out 2022.

StatsBomb Open Data attribution and licence:
https://github.com/statsbomb/open-data/blob/master/LICENSE.pdf
No odds are available in this dataset: ROI and market outperformance are unknown.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from core.live_model import payment, remaining_distribution
from core.replay import proper_scores, wilson

COMPETITION = 43
TRAIN_SEASON = 3
TEST_SEASON = 106
REPLAY_MINUTES = (15, 30, 45, 60, 75, 85)
SELECTION_THRESHOLD = .85
MAX_WORKERS = 3
SOURCE_COMMIT = '4b73468fc5b0f1950f9f66fada70ad3a4f9327cb'


def fetch(url, cache):
    if cache.exists():
        return json.loads(cache.read_text())
    request = urllib.request.Request(url, headers={'User-Agent': 'ai-football-public-research'})
    with urllib.request.urlopen(request, timeout=45) as response:
        body = response.read()
    value = json.loads(body)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(body)
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cache', required=True)
    parser.add_argument('--commit', default=SOURCE_COMMIT)
    args = parser.parse_args()
    cache = Path(args.cache)
    root = Path(__file__).resolve().parents[1]
    commit = args.commit
    base = 'https://raw.githubusercontent.com/statsbomb/open-data/' + commit + '/data/'
    seasons = {season: fetch(base + f'matches/{COMPETITION}/{season}.json', cache / f'matches-{season}.json')
               for season in (TRAIN_SEASON, TEST_SEASON)}
    all_matches = [(season, match) for season, matches in seasons.items() for match in matches]

    def reduce_match(item):
        season, match = item
        mid = match['match_id']
        events = fetch(base + f'events/{mid}.json', cache / f'events-{mid}.json')
        home_id = match['home_team']['home_team_id']
        goals = []
        for event in events:
            if event.get('period') not in (1, 2):
                continue
            name = event.get('type', {}).get('name')
            is_goal = name == 'Shot' and event.get('shot', {}).get('outcome', {}).get('name') == 'Goal'
            own_goal = name == 'Own Goal Against'
            if not (is_goal or own_goal):
                continue
            home = event.get('team', {}).get('id') == home_id
            if own_goal:
                home = not home
            goals.append((int(event['minute']) * 60 + int(event['second']), int(home)))
        goals.sort()
        ft = (sum(side for _, side in goals), sum(1 - side for _, side in goals))
        return {'season': season, 'match_id': mid, 'date': match['match_date'],
                'home': match['home_team']['home_team_name'], 'away': match['away_team']['away_team_name'],
                'ft': ft, 'goals': goals}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        matches = list(pool.map(reduce_match, all_matches))
    train = [m for m in matches if m['season'] == TRAIN_SEASON]
    test = sorted([m for m in matches if m['season'] == TEST_SEASON], key=lambda m: (m['date'], m['match_id']))
    h_rate = sum(m['ft'][0] for m in train) / len(train)
    a_rate = sum(m['ft'][1] for m in train) / len(train)
    outcomes = ('home', 'draw', 'away')
    baseline_dist = remaining_distribution(h_rate, a_rate, (0, 0))
    baseline = [payment(baseline_dist, 'HAD', oc).win for oc in outcomes]
    observations = []
    selected = []
    for match in test:
        actual = 0 if match['ft'][0] > match['ft'][1] else 2 if match['ft'][0] < match['ft'][1] else 1
        match_selected = False
        for minute in REPLAY_MINUTES:
            # Future goals are never passed to the probability model.
            seen = [(second, side) for second, side in match['goals'] if second < minute * 60]
            score = (sum(side for _, side in seen), sum(1 - side for _, side in seen))
            remaining = (90 - minute) / 90
            dist = remaining_distribution(h_rate * remaining, a_rate * remaining, score)
            probs = [payment(dist, 'HAD', oc).win for oc in outcomes]
            brier, log_loss = proper_scores(probs, actual)
            bb, bl = proper_scores(baseline, actual)
            pick = max(range(3), key=lambda i: probs[i])
            observations.append(dict(match_id=match['match_id'], minute=minute, score=f'{score[0]}:{score[1]}',
                                     actual=outcomes[actual], p_home=probs[0], p_draw=probs[1], p_away=probs[2],
                                     brier=brier, log_loss=log_loss, baseline_brier=bb, baseline_log_loss=bl,
                                     predicted=outcomes[pick], correct=int(pick == actual)))
            if not match_selected and probs[pick] >= SELECTION_THRESHOLD:
                selected.append(dict(match_id=match['match_id'], minute=minute, probability=probs[pick],
                                     correct=int(pick == actual)))
                match_selected = True
    with (root / 'data/public-replay.csv').open('w') as f:
        writer = csv.DictWriter(f, fieldnames=list(observations[0]), lineterminator='\n')
        writer.writeheader()
        writer.writerows(observations)
    with (root / 'data/public-matches.csv').open('w') as f:
        writer = csv.writer(f, lineterminator='\n')
        writer.writerow(('season', 'match_id', 'date', 'home', 'away', 'regulation_home', 'regulation_away'))
        for m in matches:
            writer.writerow((m['season'], m['match_id'], m['date'], m['home'], m['away'], *m['ft']))
    with (root / 'data/public-goals.csv').open('w') as f:
        writer = csv.writer(f, lineterminator='\n')
        writer.writerow(('season', 'match_id', 'second', 'home_goal'))
        for m in matches:
            for second, side in m['goals']:
                writer.writerow((m['season'], m['match_id'], second, side))
    hits = sum(row['correct'] for row in selected)
    summary = {'source': 'StatsBomb Open Data', 'commit': commit, 'training_season': 2018, 'test_season': 2022,
               'train_matches': len(train), 'test_matches': len(test), 'checkpoints': len(observations),
               'rates': [h_rate, a_rate], 'selection_threshold': SELECTION_THRESHOLD,
               'selected_independent_matches': len(selected), 'hits': hits,
               'coverage': len(selected) / len(test), 'hit_rate': hits / len(selected) if selected else None,
               'wilson95': wilson(hits, len(selected)) if selected else None,
               'roi': None, 'market_comparison': None,
               'limitation': 'Historical national-team baseline; no venue odds, no evidence of trading advantage; 90-minute horizon excludes unknown stoppage duration.'}
    summary['by_minute'] = {}
    for minute in REPLAY_MINUTES:
        rows = [r for r in observations if r['minute'] == minute]
        summary['by_minute'][minute] = {key: sum(r[key] for r in rows) / len(rows)
                                      for key in ('brier', 'log_loss', 'baseline_brier', 'baseline_log_loss', 'correct')}
    summary['reliability'] = []
    for bucket in range(10):
        rows = [r for r in observations if bucket / 10 <= max(r['p_home'], r['p_draw'], r['p_away']) < (bucket + 1) / 10]
        if rows:
            summary['reliability'].append({'bin': bucket, 'n': len(rows),
                                          'mean_probability': sum(max(r['p_home'], r['p_draw'], r['p_away']) for r in rows) / len(rows),
                                          'accuracy': sum(r['correct'] for r in rows) / len(rows)})
    (root / 'data/public-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
