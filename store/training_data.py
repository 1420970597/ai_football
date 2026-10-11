"""Immutable model inputs; every feature belongs to one pre-decision snapshot."""
from __future__ import annotations

from contextlib import closing

import json
import hashlib
import heapq
import math
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

from core.learned_model import DATA_VERSION, compatible_model, features
from core.decision_features import FEATURE_VERSION, verified_context, visible_history
from core.live_model import MODEL_VERSION
from core.settlement import _score_of, settle_with_basis

GRADED = ('won', 'lost', 'half_won', 'half_lost')


def snapshot_inputs(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    if (row.get('training_eligible') is False or row.get('model_version') != MODEL_VERSION or row.get('competition_type') != 'real'
            or row.get('finished') or row.get('stale') or _score_of(row.get('score')) is None):
        return []
    cutoff = row.get('input_cutoff_ms')
    if (isinstance(cutoff, bool) or not isinstance(cutoff, (int, float)) or
            not math.isfinite(cutoff) or cutoff <= 0):
        return []
    # A snapshot cutoff says when the decision was captured; it does not prove
    # when each quote became visible. New training rows require both clocks.
    decision_received = row.get('received_at_ms')
    if (isinstance(decision_received, bool) or not isinstance(decision_received, (int, float))
            or not math.isfinite(decision_received) or decision_received <= 0 or decision_received > cutoff):
        return []
    inputs = []
    evaluations = list(row.get('evaluations', []))
    if row.get('baseline_ensemble') or row.get('ensemble'):
        evaluations.append(row.get('baseline_ensemble') or row['ensemble'])
    peers: dict[tuple[str, str, str], dict[str, float]] = {}
    for evaluation in evaluations:
        for candidate in evaluation.get('candidates', []):
            key = (candidate['market'], str(candidate['line']), candidate['outcome'])
            received = candidate.get('quote_received_at_ms')
            if (candidate.get('ts_ms', float('inf')) <= cutoff and
                    isinstance(received, (int, float)) and not isinstance(received, bool) and math.isfinite(received) and
                    0 < received <= cutoff):
                peers.setdefault(key, {})[evaluation['algorithm']] = candidate['p_model']
    for evaluation in evaluations:
        for candidate in evaluation.get('candidates', []):
            if (candidate.get('research_only') or candidate.get('decision_status') != 'settleable'
                    or candidate.get('ts_ms', float('inf')) > cutoff
                    or isinstance(candidate.get('quote_received_at_ms'), bool)
                    or not isinstance(candidate.get('quote_received_at_ms'), (int, float))
                    or not math.isfinite(candidate.get('quote_received_at_ms'))
                    or candidate.get('quote_received_at_ms', 0) <= 0
                    or candidate.get('quote_received_at_ms', float('inf')) > cutoff):
                continue
            try:
                # Reuse the online as-of filter. Besides event time, richer
                # history rows carry their first-received time in slot 2; a
                # quote that happened before cutoff but arrived afterwards is
                # not part of the information set for this decision.
                history = visible_history({**candidate, 'input_cutoff_ms': cutoff})
                trend = (history[-1][1] / history[0][1] - 1) * 100 if len(history) > 1 else None
                x = features({**candidate, 'training_history': history, 'p_fused': candidate['p_model'],
                              'algorithm_probabilities': peers.get((candidate['market'],str(candidate['line']),candidate['outcome']), {}),
                              'algorithm': evaluation['algorithm'], 'entry_score': row['score'],
                              'entry_clock_s': row['elapsed_s'],
                              'training_trend_pct': trend, 'input_cutoff_ms': cutoff,
                              'feature_context': verified_context(row)})
            except (ValueError, TypeError, KeyError):
                continue
            inputs.append({'match_id': row['match_id'], 'at': row['computed_at'], 'cutoff_ms': cutoff,
                           'decision_received_at_ms': decision_received,
                           'quote_received_at_ms': candidate['quote_received_at_ms'],
                           'algorithm': evaluation['algorithm'], 'market': candidate['market'],
                           'line': str(candidate['line']), 'outcome': candidate['outcome'],
                           'baseline': candidate['p_market'], 'x': json.dumps(x),
                           'entry_score': json.dumps(row['score']), 'settlement_basis': candidate.get('settlement_basis', 'unknown'),
                           'model_version': MODEL_VERSION, 'data_version': DATA_VERSION,
                           'feature_version': FEATURE_VERSION})
    return inputs


def write_inputs(path: Path, rows: list[dict[str, Any]], *, repair: bool = False) -> None:
    if not rows:
        return
    # Repair/test callers may provide a v3 vector without repeating the schema
    # marker; normalize it once at the storage boundary.
    rows = [{**row, 'feature_version': row.get('feature_version', FEATURE_VERSION),
             'decision_received_at_ms': row.get('decision_received_at_ms'),
             'quote_received_at_ms': row.get('quote_received_at_ms')} for row in rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=2)) as con, con:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA synchronous=FULL')
        con.execute('CREATE TABLE IF NOT EXISTS inputs (match_id TEXT,at TEXT,cutoff_ms REAL,algorithm TEXT,'
                    'market TEXT,line TEXT,outcome TEXT,baseline REAL,x TEXT,'
                    'decision_received_at_ms REAL,quote_received_at_ms REAL,'
                    'PRIMARY KEY(match_id,at,algorithm,market,line,outcome))')
        con.execute('CREATE INDEX IF NOT EXISTS idx_inputs_at ON inputs(at)')
        migrate_basis_columns(con, 'inputs')
        con.execute('CREATE INDEX IF NOT EXISTS idx_inputs_version ON inputs(data_version,model_version,at)')
        for name in ('decision_received_at_ms', 'quote_received_at_ms'):
            existing = {r[1] for r in con.execute('PRAGMA table_info(inputs)')}
            if name not in existing:
                con.execute('ALTER TABLE inputs ADD COLUMN ' + name + ' REAL')
        columns = 'match_id,at,cutoff_ms,algorithm,market,line,outcome,baseline,x,decision_received_at_ms,quote_received_at_ms,entry_score,settlement_basis,model_version,data_version,feature_version'
        con.executemany(('INSERT OR REPLACE' if repair else 'INSERT OR IGNORE') + ' INTO inputs (' + columns + ') VALUES (' +
                        ','.join(':' + name for name in columns.split(',')) + ')', rows)


BASIS_COLUMNS = ('entry_score', 'settlement_basis', 'model_version', 'data_version', 'feature_version')


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
    # Select the latest explicitly available revision per match. Never infer
    # availability from settled_at: old repaired rows may have been discovered
    # much later than the event they describe.
    has_revisions = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='label_revisions'").fetchone()
    if has_revisions:
        source = con.execute("SELECT match_id,available_at,ft_score,ht_score FROM label_revisions "
                             "ORDER BY revision_order,revision_id")
    else:
        # Older projections may not have the indexed column yet. Read the
        # explicit point-in-time timestamp from the immutable payload; never
        # substitute settled_at for repaired legacy records.
        source = con.execute("SELECT match_id,json_extract(payload,'$.label_available_at'),"
            "json_extract(payload,'$.ft_score'),json_extract(payload,'$.ht_score') "
            "FROM decisions WHERE competition_type='real' "
            "AND status IN ('won','lost','half_won','half_lost','push','void') "
            "AND json_extract(payload,'$.label_available_at') IS NOT NULL "
            "AND json_extract(payload,'$.label_available_at')!='' "
            "AND json_extract(payload,'$.ft_score') IS NOT NULL "
            "ORDER BY json_extract(payload,'$.label_available_at')")
    for mid, at, ft, ht in source:
        ft_score = _score_of(json.loads(ft) if isinstance(ft, str) else ft)
        ht_score = _score_of(json.loads(ht) if isinstance(ht, str) and ht else ht) if ht else None
        if ft_score is not None:
            previous = result.get(str(mid), {})
            if at >= previous.get('settled_at', ''):
                result[str(mid)] = {'ft_score': ft_score, 'ht_score': ht_score,
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


def dataset(db: Path, inputs_path: Path | None = None, *, audit: dict[str, Any] | None = None,
            max_per_match: int | None = None) -> list[dict[str, Any]]:
    if max_per_match is not None and (isinstance(max_per_match, bool) or not isinstance(max_per_match, int) or max_per_match < 1):
        raise ValueError('每场训练样本上限必须为正整数')
    if not db.exists():
        return []
    con = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    rows = []
    seen: set[bytes] = set()
    counts = {'input_records': 0, 'valid_training_decisions': 0, 'waiting_for_label': 0,
              'quarantined_inputs': 0, 'quarantined_ledger': 0, 'ungraded_inputs': 0,
              'quarantined_missing_time_contract': 0, 'eligible_time_contract': 0}
    buckets: dict[str, list] = {}
    source_count = 0
    def collect(row: dict[str, Any], identity: tuple) -> None:
        nonlocal source_count
        source_count += 1
        if max_per_match is None:
            rows.append(row)
            return
        # Deterministic min-hash sampling depends only on pre-result identity.
        # Restore every input, but bound training memory/CPU without favoring wins.
        priority = int.from_bytes(hashlib.sha256(json.dumps(identity).encode()).digest()[:8], 'big')
        bucket = buckets.setdefault(row['match_id'], [])
        item = (-priority, source_count, row)
        if len(bucket) < max_per_match:
            heapq.heappush(bucket, item)
        elif item[0] > bucket[0][0]:
            heapq.heapreplace(bucket, item)
    try:
        finals = labels(con)
        if inputs_path and inputs_path.exists():
            with closing(sqlite3.connect(inputs_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as inputs:
                inputs.row_factory = sqlite3.Row
                has_inputs = inputs.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inputs'").fetchone()
                if has_inputs:
                    counts['input_records'] = inputs.execute('SELECT count(*) FROM inputs').fetchone()[0]
                columns = {r[1] for r in inputs.execute('PRAGMA table_info(inputs)')}
                source: Iterable[sqlite3.Row]
                if set(BASIS_COLUMNS) <= columns:
                    counts['quarantined_inputs'] = inputs.execute(
                        "SELECT count(*) FROM inputs WHERE data_version IS NOT ? OR model_version IS NOT ? OR feature_version IS NOT ?",
                        (DATA_VERSION, MODEL_VERSION, FEATURE_VERSION)).fetchone()[0]
                    source = inputs.execute('SELECT * FROM inputs WHERE data_version=? AND model_version=? AND feature_version=? ORDER BY at',
                                            (DATA_VERSION, MODEL_VERSION, FEATURE_VERSION))
                else:
                    counts['quarantined_inputs'] = inputs.execute('SELECT count(*) FROM inputs').fetchone()[0]
                    source = iter(())
                for r in source:
                    row = dict(r)
                    clocks = (row.get('decision_received_at_ms'), row.get('quote_received_at_ms'), row.get('cutoff_ms'))
                    if (any(isinstance(value, bool) or not isinstance(value, (int, float)) or
                            not math.isfinite(value) or value <= 0 for value in clocks) or
                            row['decision_received_at_ms'] > row['cutoff_ms'] or
                            row['quote_received_at_ms'] > row['cutoff_ms']):
                        counts['quarantined_missing_time_contract'] += 1
                        continue
                    counts['eligible_time_contract'] += 1
                    final = finals.get(row['match_id'])
                    if not final or final['settled_at'] <= row['at']:
                        counts['waiting_for_label'] += 1
                        continue
                    paid = target(row, final)
                    if paid is None:
                        counts['ungraded_inputs'] += 1
                        continue
                    y, weight = paid
                    key = tuple(row[k] for k in ('match_id', 'at', 'algorithm', 'market', 'line', 'outcome'))
                    collect({'match_id': row['match_id'], 'at': row['at'], 'settled_at': final['settled_at'],
                                 'x': json.loads(row['x']), 'y': y, 'observation_weight': weight,
                                 'baseline': row['baseline'], 'algorithm': row['algorithm'],
                                 'market': row['market'], 'line': row['line'], 'outcome': row['outcome']}, key)
                    counts['valid_training_decisions'] += 1
                    seen.add(hashlib.sha256(json.dumps(key).encode()).digest()[:16])
        # Only corrected model generations can supply fallback features. Old peer logits
        # and predictions cannot be repaired by relabeling their final outcomes.
        counts['quarantined_ledger'] = con.execute("SELECT count(*) FROM decisions WHERE competition_type='real' "
                "AND trigger IN ('live_forecast','live_recommendation') AND "
                "model_version!=?", (MODEL_VERSION,)).fetchone()[0]
        # Ledger payloads are not a certified feature snapshot: they may lack
        # quote-received clocks and the frozen replay schema. Never let this
        # convenience fallback bypass the dual-time training contract.
        counts['quarantined_ledger'] += con.execute("SELECT count(*) FROM decisions WHERE competition_type='real' "
                "AND trigger IN ('live_forecast','live_recommendation') AND research_only=0 "
                "AND model_version=? AND status IN ('won','lost','half_won','half_lost')", (MODEL_VERSION,)).fetchone()[0]
        if audit is not None:
            invalid = (counts['quarantined_inputs'] + counts['quarantined_ledger'] +
                       counts['quarantined_missing_time_contract'] + counts['ungraded_inputs'])
            audit.update(counts, invalid_training_records=invalid,
                         source_eligible_decisions=source_count,
                         training_max_rows_per_match=max_per_match)
        if max_per_match is not None:
            rows = sorted((item[2] for bucket in buckets.values() for item in bucket), key=lambda r:r['at'])
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
                    or final['settled_at'] <= predicted_at
                    or not model.get('updated_at') or row['at'] <= model['updated_at']):
                continue
            paid = target(row, final)
            if paid is None:
                continue
            y, weight = paid
            evaluated.append({'match_id': row['match_id'], 'x': json.loads(row['x']),
                              'baseline': row['baseline'], 'y': y, 'observation_weight': weight})
    return {'version': model['version'], 'data_version': DATA_VERSION, 'model': metrics(evaluated, model['weights']),
            'baseline': metrics(evaluated, None)} if evaluated else {}
