#!/usr/bin/env python3
"""Reproduce descriptive statistics; public pre-match data never produce ROI."""
from __future__ import annotations

import argparse
import csv
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

REPORT = Path(__file__).resolve().parents[1]
REPO = REPORT.parents[1]
HISTORY = REPO / 'reports/independent-strategy-audit/data/real_orders_joined.csv'
GRADED = {'won', 'lost', 'half_won', 'half_lost', 'push'}
OUTCOMES = ('H', 'D', 'A')
LEAGUES = {'E0': '英超', 'D1': '德甲', 'I1': '意甲', 'SP1': '西甲', 'F1': '法甲'}
OVERROUNDS = (0.02, 0.05, 0.10, 0.15)
ODDS_LEVELS = (1.5, 2.0, 3.0)
PROBABILITY_ERRORS = (0.01, 0.02, 0.05)
POOLING_COUNTS = (10, 30, 100, 300, 1000)
POOLING_PRIORS = (50, 200, 500)
WITHIN_MATCH_CORRELATIONS = (0.0, 0.1, 0.5, 0.9, 1.0)
TICKS_PER_MATCH = 40


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError('empty output')
    with path.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)


def verified_bytes(path: Path, expected: str) -> bytes:
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected:
        raise ValueError(f'snapshot hash mismatch: {path.name}')
    return content


def fair_probabilities(odds: list[float]) -> tuple[list[float], float]:
    if len(odds) != 3 or any(not math.isfinite(o) or o <= 1 for o in odds):
        raise ValueError('three finite decimal odds > 1 required')
    implied = [1 / o for o in odds]
    total = sum(implied)
    return [p / total for p in implied], total - 1


def historical_summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    graded = [r for r in rows if r['sim_status'] in GRADED]
    if not graded:
        raise ValueError('no graded historical orders')

    def aggregate(items: list[dict[str, str]]) -> dict[str, Any]:
        stake = sum((Decimal(r['stake']) for r in items), Decimal(0))
        net = sum((Decimal(r['stake']) * Decimal(r['sim_pnl_per_unit']) for r in items), Decimal(0))
        if stake <= 0:
            raise ValueError('non-positive graded stake')
        return {'orders': len(items), 'matches': len({r['match_id'] for r in items}),
                'stake': str(stake), 'net_recomputed': str(net), 'roi_recomputed': float(net / stake)}

    markets = [{'market': m, **aggregate([r for r in graded if r['market'] == m])}
               for m in sorted({r['market'] for r in graded})]
    result = {'all_rows': len(rows), 'pending_excluded': sum(r['sim_status'] == 'pending' for r in rows),
              **aggregate(graded), 'by_market': markets,
              'basis': 'historical decision odds and local settlement; not venue authoritative P&L'}
    assert sum(Decimal(r['stake']) for r in markets) == Decimal(result['stake'])
    assert sum(Decimal(r['net_recomputed']) for r in markets) == Decimal(result['net_recomputed'])
    return result


def league_summary(code: str, rows: list[dict[str, str]]) -> dict[str, Any]:
    if not rows:
        raise ValueError('empty league')
    goals, results, briers, losses, margins = [], [], [], [], []
    for row in rows:
        home, away = int(row['FTHG']), int(row['FTAG'])
        if home < 0 or away < 0:
            raise ValueError('negative goals')
        outcome = 'H' if home > away else 'A' if away > home else 'D'
        if row['FTR'] != outcome:
            raise ValueError('inconsistent result')
        goals.append(home + away)
        results.append(outcome)
        try:
            odds = [float(row['B365' + o]) for o in OUTCOMES]
        except (KeyError, TypeError, ValueError):
            continue
        try:
            ps, margin = fair_probabilities(odds)
        except ValueError:
            continue
        briers.append(sum((p - float(o == outcome)) ** 2 for p, o in zip(ps, OUTCOMES)))
        losses.append(-math.log(ps[OUTCOMES.index(outcome)]))
        margins.append(margin)
    n = len(rows)
    return {'league_code': code, 'league': LEAGUES[code], 'season': '2024/25', 'matches': n,
            'goals_per_match': round(sum(goals) / n, 6),
            'home_rate': round(results.count('H') / n, 6),
            'draw_rate': round(results.count('D') / n, 6),
            'away_rate': round(results.count('A') / n, 6),
            'over25_rate': round(sum(g > 2.5 for g in goals) / n, 6),
            'odds_complete': len(margins),
            'mean_overround': round(sum(margins) / len(margins), 6) if margins else None,
            'market_brier_sum3': round(sum(briers) / len(briers), 6) if briers else None,
            'market_logloss': round(sum(losses) / len(losses), 6) if losses else None}


def analyze(source_dir: Path, *, fetch: bool = False) -> dict[str, Any]:
    manifest = json.loads((REPORT / 'data/input_manifest.json').read_text())
    verified_bytes(HISTORY, manifest['history']['sha256'])
    with HISTORY.open() as f:
        historical = historical_summary(list(csv.DictReader(f)))
    public = []
    for spec in manifest['public']:
        path = source_dir / (spec['id'] + '.csv')
        if fetch:
            source_dir.mkdir(parents=True, exist_ok=True)
            with urlopen(Request(spec['url'], headers={'User-Agent': 'Mozilla/5.0 research'}), timeout=30) as response:
                content = response.read(4_000_000)
            if hashlib.sha256(content).hexdigest() != spec['sha256']:
                raise ValueError(f'upstream changed; preserve pinned snapshot: {spec["id"]}')
            path.write_bytes(content)
        verified_bytes(path, spec['sha256'])
        with path.open(encoding='utf-8-sig', newline='') as f:
            public.append(league_summary(spec['id'], list(csv.DictReader(f))))
    write_csv(REPORT / 'data/historical_by_market.csv', historical['by_market'])
    write_csv(REPORT / 'data/public_league_profiles.csv', public)
    write_csv(REPORT / 'data/margin_identity.csv', [
        {'overround': m, 'same_quote_ev': 1 / (1 + m) - 1, 'basis': 'theory, proportional devig'}
        for m in OVERROUNDS])
    write_csv(REPORT / 'data/probability_sensitivity.csv', [
        {'decimal_odds': o, 'probability_error': e, 'ev_error': o * e, 'basis': 'theory, no-push'}
        for o in ODDS_LEVELS for e in PROBABILITY_ERRORS])
    write_csv(REPORT / 'data/pooling_sensitivity.csv', [
        {'matches': n, 'prior_strength': k, 'league_weight': n / (n + k),
         'basis': 'illustration only; prior not fitted'}
        for k in POOLING_PRIORS for n in POOLING_COUNTS])
    write_csv(REPORT / 'data/tick_correlation.csv', [
        {'ticks': TICKS_PER_MATCH, 'assumed_within_match_correlation': rho,
         'effective_observations_per_match': TICKS_PER_MATCH / (1 + (TICKS_PER_MATCH - 1) * rho),
         'basis': 'equal-correlation illustration; no measured rho'}
        for rho in WITHIN_MATCH_CORRELATIONS])
    summary = {'history': historical, 'public_matches': sum(r['matches'] for r in public),
               'public_odds_complete': sum(r['odds_complete'] for r in public),
               'public_basis': 'descriptive pre-match 1X2; no strategy ROI or fitted model'}
    (REPORT / 'data/summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, required=True, help='raw public CSV cache outside tracked report')
    parser.add_argument('--fetch', action='store_true', help='download and verify pinned public sources')
    args = parser.parse_args()
    print(json.dumps(analyze(args.source_dir, fetch=args.fetch), ensure_ascii=False, indent=2))
