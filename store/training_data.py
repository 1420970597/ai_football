"""Immutable model inputs; every feature belongs to one pre-decision snapshot."""
from __future__ import annotations

from contextlib import closing

import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

from core.learned_model import DATA_VERSION, compatible_model, features
from core.live_model import MODEL_VERSION
from core.settlement import _score_of, settle_with_basis

GRADED = ('won', 'lost', 'half_won', 'half_lost')


def snapshot_inputs(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    if (row.get('model_version') != MODEL_VERSION or row.get('competition_type') != 'real'
            or row.get('finished') or row.get('stale') or _score_of(row.get('score')) is None):
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
                           'baseline': candidate['p_market'], 'x': json.dumps(x),
                           'entry_score': json.dumps(row['score']), 'settlement_basis': candidate.get('settlement_basis', 'unknown'),
                           'model_version': MODEL_VERSION, 'data_version': DATA_VERSION})
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
        migrate_basis_columns(con, 'inputs')
        con.execute('CREATE INDEX IF NOT EXISTS idx_inputs_version ON inputs(data_version,model_version,at)')
        columns = 'match_id,at,cutoff_ms,algorithm,market,line,outcome,baseline,x,entry_score,settlement_basis,model_version,data_version'
        con.executemany('INSERT OR IGNORE INTO inputs (' + columns + ') VALUES (' +
                        ','.join(':' + name for name in columns.split(',')) + ')', rows)


BASIS_COLUMNS = ('entry_score', 'settlement_basis', 'model_version', 'data_version')


def migrate_basis_columns(con: sqlite3.Connection, table: str) -> None:
    if table not in ('inputs', 'predictions'):
        raise ValueError('不支持的训练表')
    existing = {r[1] for r in con.execute('PRAGMA table_info(' + table + ')')}
    for name in BASIS_COLUMNS:
        if name not in existing:
            # NULL old values remain quarantined. A migration cannot certify old features.
            con.execute('ALTER TABLE ' + table + ' ADD COLUMN ' + name + ' TEXT')


def labels(con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    # Project only score evidence, rather than loading all historical decision payloads.
    for mid, at, ft, ht in con.execute("SELECT match_id,settled_at,json_extract(payload,'$.ft_score'),"
            "json_extract(payload,'$.ht_score') FROM decisions WHERE competition_type='real' "
            "AND status IN ('won','lost','half_won','half_lost','push','void') "
            "AND settled_at!='' AND json_extract(payload,'$.ft_score') IS NOT NULL ORDER BY settled_at"):
        ft_score = _score_of(json.loads(ft))
        ht_score = _score_of(json.loads(ht)) if ht else None
        if ft_score is not None:
            previous = result.get(str(mid), {})
            result[str(mid)] = {'ft_score': ft_score, 'ht_score': ht_score or previous.get('ht_score'),
                                'settled_at': at}
    return result


def target(row: Mapping[str, Any], final: Mapping[str, Any]) -> tuple[int, float] | None:
    entry = row.get('entry_score')
    if isinstance(entry, str):
        try:
            entry = json.loads(entry)
        except ValueError:
            return None
    expected = ('remaining_score' if str(row['market']).startswith('AH') else
                'half_score_required' if str(row['market']).endswith('_1H') else 'full_score')
    if row.get('settlement_basis') != expected:
        return None
    status, _ = settle_with_basis(row['market'], row['outcome'], row['line'], final['ft_score'],
                                 final.get('ht_score'), settlement_basis=expected, entry_score=entry)
    if status not in GRADED:
        return None
    return int(status in ('won', 'half_won')), .5 if status.startswith('half_') else 1.0


def dataset(db: Path, inputs_path: Path | None = None, *, audit: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if not db.exists():
        return []
    con = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    rows = []
    seen: set[tuple[str, ...]] = set()
    counts = {'quarantined_inputs': 0, 'quarantined_ledger': 0, 'ungraded_inputs': 0}
    try:
        finals = labels(con)
        if inputs_path and inputs_path.exists():
            with closing(sqlite3.connect(inputs_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as inputs:
                inputs.row_factory = sqlite3.Row
                columns = {r[1] for r in inputs.execute('PRAGMA table_info(inputs)')}
                source: Iterable[sqlite3.Row]
                if set(BASIS_COLUMNS) <= columns:
                    counts['quarantined_inputs'] = inputs.execute(
                        "SELECT count(*) FROM inputs WHERE data_version IS NOT ? OR model_version IS NOT ?",
                        (DATA_VERSION, MODEL_VERSION)).fetchone()[0]
                    source = inputs.execute('SELECT * FROM inputs WHERE data_version=? AND model_version=? ORDER BY at',
                                            (DATA_VERSION, MODEL_VERSION))
                else:
                    counts['quarantined_inputs'] = inputs.execute('SELECT count(*) FROM inputs').fetchone()[0]
                    source = iter(())
                for r in source:
                    row = dict(r)
                    final = finals.get(row['match_id'])
                    if not final or final['settled_at'] <= row['at']:
                        continue
                    paid = target(row, final)
                    if paid is None:
                        counts['ungraded_inputs'] += 1
                        continue
                    y, weight = paid
                    rows.append({'match_id': row['match_id'], 'at': row['at'], 'settled_at': final['settled_at'],
                                 'x': json.loads(row['x']), 'y': y, 'observation_weight': weight,
                                 'baseline': row['baseline']})
                    seen.add(tuple(row[k] for k in ('match_id', 'at', 'algorithm', 'market', 'line', 'outcome')))
        # Only corrected model generations can supply fallback features. Old peer logits
        # and predictions cannot be repaired by relabeling their final outcomes.
        counts['quarantined_ledger'] = con.execute("SELECT count(*) FROM decisions WHERE competition_type='real' "
                "AND trigger IN ('live_forecast','live_recommendation') AND "
                "model_version!=?", (MODEL_VERSION,)).fetchone()[0]
        for (payload,) in con.execute("SELECT payload FROM decisions WHERE competition_type='real' "
                "AND trigger IN ('live_forecast','live_recommendation') AND research_only=0 "
                "AND model_version=? "
                "AND status IN ('won','lost','half_won','half_lost') ORDER BY at,identity", (MODEL_VERSION,)):
            r = json.loads(payload)
            key = tuple(str(r.get(k, '')) for k in ('match_id', 'at', 'algorithm', 'market', 'line', 'outcome'))
            if key in seen:
                continue
            try:
                final = finals.get(str(r['match_id']))
                if not final or final['settled_at'] <= r['at']:
                    continue
                paid = target(r, final)
                if paid is None:
                    continue
                y, weight = paid
                rows.append({'match_id': r['match_id'], 'at': r['at'], 'settled_at': final['settled_at'],
                             'x': features(r), 'y': y, 'observation_weight': weight, 'baseline': r['p_market']})
                seen.add(key)
            except (ValueError, TypeError, KeyError):
                continue
        if audit is not None:
            audit.update(counts, data_version=DATA_VERSION)
        return rows
    finally:
        con.close()


def prospective_metrics(db: Path, path: Path, model: Mapping[str, Any]) -> dict[str, Any]:
    from core.learned_model import metrics
    if not compatible_model(model) or not db.exists() or not path.exists():
        return {}
    with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as con:
        finals = labels(con)
    evaluated = []
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as con:
        con.row_factory = sqlite3.Row
        columns = {r[1] for r in con.execute('PRAGMA table_info(predictions)')}
        if not set(BASIS_COLUMNS) <= columns or 'predicted_at' not in columns:
            return {}
        for raw in con.execute('SELECT * FROM predictions WHERE version=? AND data_version=? AND model_version=?',
                               (model['version'], DATA_VERSION, MODEL_VERSION)):
            row = dict(raw)
            final = finals.get(row['match_id'])
            predicted_at = row['predicted_at']
            if (not final or not predicted_at or final['settled_at'] <= row['at']
                    or final['settled_at'] <= predicted_at):
                continue
            paid = target(row, final)
            if paid is None:
                continue
            y, weight = paid
            evaluated.append({'match_id': row['match_id'], 'x': json.loads(row['x']),
                              'baseline': row['baseline'], 'y': y, 'observation_weight': weight})
    return {'version': model['version'], 'data_version': DATA_VERSION, 'model': metrics(evaluated, model['weights']),
            'baseline': metrics(evaluated, None)} if evaluated else {}
