"""Immutable model inputs; every feature belongs to one pre-decision snapshot."""
from __future__ import annotations

from contextlib import closing

import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from core.learned_model import features
from core.settlement import settle_pick

GRADED = ('won', 'lost', 'half_won', 'half_lost')


def snapshot_inputs(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    if row.get('competition_type') != 'real' or row.get('finished') or row.get('stale'):
        return []
    cutoff = row.get('input_cutoff_ms')
    if isinstance(cutoff, bool) or not isinstance(cutoff, (int, float)):
        return []
    inputs = []
    evaluations = list(row.get('evaluations', []))
    if row.get('ensemble'):
        evaluations.append(row['ensemble'])
    peers: dict[tuple[str, str, str], dict[str, float]] = {}
    for evaluation in evaluations:
        for candidate in evaluation.get('candidates', []):
            key = (candidate['market'], str(candidate['line']), candidate['outcome'])
            if candidate.get('ts_ms', float('inf')) <= cutoff:
                peers.setdefault(key, {})[evaluation['algorithm']] = candidate['p_model']
    for evaluation in evaluations:
        for candidate in evaluation.get('candidates', []):
            if (candidate.get('research_only') or candidate.get('decision_status') != 'settleable'
                    or candidate.get('ts_ms', float('inf')) > cutoff):
                continue
            try:
                history = [point for point in candidate.get('training_history', [])
                           if isinstance(point, (list, tuple)) and len(point) == 2 and point[0] <= cutoff]
                trend = (history[-1][1] / history[0][1] - 1) * 100 if len(history) > 1 else None
                x = features({**candidate, 'training_history': history, 'p_fused': candidate['p_model'],
                              'algorithm_probabilities': peers.get((candidate['market'],str(candidate['line']),candidate['outcome']), {}),
                              'algorithm': evaluation['algorithm'], 'entry_score': row['score'],
                              'entry_clock_s': row['elapsed_s'],
                              'training_trend_pct': trend})
            except (ValueError, TypeError, KeyError):
                continue
            inputs.append({'match_id': row['match_id'], 'at': row['computed_at'], 'cutoff_ms': cutoff,
                           'algorithm': evaluation['algorithm'], 'market': candidate['market'],
                           'line': str(candidate['line']), 'outcome': candidate['outcome'],
                           'baseline': candidate['p_market'], 'x': json.dumps(x)})
    return inputs


def write_inputs(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=2)) as con, con:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA synchronous=FULL')
        con.execute('CREATE TABLE IF NOT EXISTS inputs (match_id TEXT,at TEXT,cutoff_ms REAL,algorithm TEXT,'
                    'market TEXT,line TEXT,outcome TEXT,baseline REAL,x TEXT,'
                    'PRIMARY KEY(match_id,at,algorithm,market,line,outcome))')
        con.execute('CREATE INDEX IF NOT EXISTS idx_inputs_at ON inputs(at)')
        con.executemany('INSERT OR IGNORE INTO inputs VALUES '
                        '(:match_id,:at,:cutoff_ms,:algorithm,:market,:line,:outcome,:baseline,:x)', rows)


def labels(con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    result = {}
    for (payload,) in con.execute("SELECT payload FROM decisions WHERE competition_type='real' "
                                 "AND status IN ('won','lost','half_won','half_lost','push') ORDER BY settled_at"):
        row = json.loads(payload)
        if row.get('ft_score') and row.get('settled_at'):
            result[str(row['match_id'])] = row
    return result


def dataset(db: Path, inputs_path: Path | None = None) -> list[dict[str, Any]]:
    if not db.exists():
        return []
    con = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    rows = []
    seen: set[tuple[str, ...]] = set()
    try:
        finals = labels(con)
        if inputs_path and inputs_path.exists():
            with closing(sqlite3.connect(inputs_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as inputs, inputs:
                inputs.row_factory = sqlite3.Row
                for r in inputs.execute('SELECT * FROM inputs ORDER BY at'):
                    final = finals.get(r['match_id'])
                    if not final or final['settled_at'] <= r['at']:
                        continue
                    status, _ = settle_pick(r['market'], r['outcome'], r['line'], final['ft_score'], final.get('ht_score'))
                    if status not in GRADED:
                        continue
                    rows.append({'match_id': r['match_id'], 'at': r['at'], 'settled_at': final['settled_at'],
                                 'x': json.loads(r['x']), 'y': int(status in ('won', 'half_won')),
                                 'baseline': r['baseline']})
                    seen.add((r['match_id'], r['at'], r['algorithm'], r['market'], r['line'], r['outcome']))
        # Legacy immutable entries preserve every available market/line/algorithm. No future trend join.
        for (payload,) in con.execute("SELECT payload FROM decisions WHERE competition_type='real' "
                "AND trigger IN ('live_forecast','live_recommendation') AND research_only=0 "
                "AND status IN ('won','lost','half_won','half_lost') ORDER BY at,identity"):
            r = json.loads(payload)
            key = tuple(str(r.get(k, '')) for k in ('match_id', 'at', 'algorithm', 'market', 'line', 'outcome'))
            if key in seen:
                continue
            try:
                if not r.get('ft_score') or not r.get('settled_at') or r['settled_at'] <= r['at']:
                    continue
                # Older ledger does not retain snapshot trend; keep explicit missingness.
                x = features(r)
                rows.append({'match_id': r['match_id'], 'at': r['at'], 'settled_at': r['settled_at'],
                             'x': x, 'y': int(r['status'] in ('won', 'half_won')), 'baseline': r['p_market']})
                seen.add(key)
            except (ValueError, TypeError, KeyError):
                continue
        return rows
    finally:
        con.close()


def prospective_metrics(db: Path, path: Path, model: Mapping[str, Any]) -> dict[str, Any]:
    from core.learned_model import metrics
    if not model or not db.exists() or not path.exists():
        return {}
    with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as con, con:
        finals = labels(con)
    evaluated = []
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as con, con:
        con.row_factory = sqlite3.Row
        for row in con.execute('SELECT * FROM predictions WHERE version=?', (model['version'],)):
            final = finals.get(row['match_id'])
            if not final or final['settled_at'] <= row['at']:
                continue
            status, _ = settle_pick(row['market'], row['outcome'], row['line'], final['ft_score'], final.get('ht_score'))
            if status not in GRADED:
                continue
            evaluated.append({'match_id': row['match_id'], 'x': json.loads(row['x']),
                              'baseline': row['baseline'], 'y': int(status in ('won','half_won'))})
    return {'version': model['version'], 'model': metrics(evaluated, model['weights']),
            'baseline': metrics(evaluated, None)} if evaluated else {}
