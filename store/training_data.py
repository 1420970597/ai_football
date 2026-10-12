"""Immutable model inputs; every feature belongs to one pre-decision snapshot."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone

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
from store.training_projection import (
    ProjectionTarget,
    ensure_postgres_schema,
    projection_target_from_env,
    readonly_snapshot,
    writable_connection,
)

GRADED = ('won', 'lost', 'half_won', 'half_lost')
TARGET_CACHE_LIMIT = 100_000


def _label_time(value: Any) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(value))
        return at.astimezone(timezone.utc) if at.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


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


def _normalise_input_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Repair/test callers may provide a v3 vector without repeating the schema
    # marker; normalize it once at the storage boundary.
    return [{**row, 'feature_version': row.get('feature_version', FEATURE_VERSION),
             'decision_received_at_ms': row.get('decision_received_at_ms'),
             'quote_received_at_ms': row.get('quote_received_at_ms')} for row in rows]


def write_inputs(path: Path, rows: list[dict[str, Any]], *, repair: bool = False,
                 target: ProjectionTarget | None = None) -> None:
    if not rows:
        return
    rows = _normalise_input_rows(rows)
    target = target or projection_target_from_env()
    if target is not None:
        _write_inputs_postgres(rows, repair=repair, target=target)
        return
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
        # The training worker filters by the immutable schema before it joins
        # labels.  Keep the match key in the same index so the join and the
        # per-match sampling pass do not repeatedly scan the JSON payload.
        con.execute('CREATE INDEX IF NOT EXISTS idx_inputs_training '
                    'ON inputs(data_version,model_version,feature_version,match_id,at)')
        for name in ('decision_received_at_ms', 'quote_received_at_ms'):
            existing = {r[1] for r in con.execute('PRAGMA table_info(inputs)')}
            if name not in existing:
                con.execute('ALTER TABLE inputs ADD COLUMN ' + name + ' REAL')
        columns = 'match_id,at,cutoff_ms,algorithm,market,line,outcome,baseline,x,decision_received_at_ms,quote_received_at_ms,entry_score,settlement_basis,model_version,data_version,feature_version'
        con.executemany(('INSERT OR REPLACE' if repair else 'INSERT OR IGNORE') + ' INTO inputs (' + columns + ') VALUES (' +
                        ','.join(':' + name for name in columns.split(',')) + ')', rows)


INPUT_COLUMNS = ('match_id', 'at', 'cutoff_ms', 'algorithm', 'market', 'line', 'outcome',
                 'baseline', 'x', 'decision_received_at_ms', 'quote_received_at_ms',
                 'entry_score', 'settlement_basis', 'model_version', 'data_version',
                 'feature_version')


def _write_inputs_postgres(rows: list[dict[str, Any]], *, repair: bool,
                           target: ProjectionTarget) -> None:
    if target.backend != 'postgresql':
        raise ValueError('训练输入写入目标必须是 PostgreSQL')
    with writable_connection(target) as con:
        ensure_postgres_schema(con)
        placeholders = ','.join('%s' for _ in INPUT_COLUMNS)
        columns = ','.join(INPUT_COLUMNS)
        action = ('DO UPDATE SET ' + ','.join(f'{name}=EXCLUDED.{name}' for name in INPUT_COLUMNS[1:])
                  if repair else 'DO NOTHING')
        sql = (f'INSERT INTO model_inputs ({columns}) VALUES ({placeholders}) '
               f'ON CONFLICT (match_id,at,algorithm,market,line,outcome) {action}')
        cursor = con.cursor()
        try:
            values = [tuple(row.get(name) for name in INPUT_COLUMNS) for row in rows]
            cursor.executemany(sql, values)
        finally:
            cursor.close()


PREDICTION_COLUMNS = ('version', 'match_id', 'at', 'cutoff_ms', 'algorithm', 'market', 'line',
                      'outcome', 'baseline', 'x', 'probability', 'predicted_at', 'entry_score',
                      'settlement_basis', 'model_version', 'data_version', 'feature_version',
                      'decision_received_at_ms', 'quote_received_at_ms')


def write_predictions(path: Path, rows: Iterable[Mapping[str, Any]], *,
                      target: ProjectionTarget | None = None) -> None:
    """Persist shadow predictions to PostgreSQL or the legacy SQLite file."""
    values = [dict(row) for row in rows]
    if not values:
        return
    target = target or projection_target_from_env()
    if target is not None:
        if target.backend != 'postgresql':
            raise ValueError('训练预测写入目标必须是 PostgreSQL')
        with writable_connection(target) as con:
            ensure_postgres_schema(con)
            columns = ','.join(PREDICTION_COLUMNS)
            placeholders = ','.join('%s' for _ in PREDICTION_COLUMNS)
            sql = (f'INSERT INTO model_predictions ({columns}) VALUES ({placeholders}) '
                   'ON CONFLICT (version,match_id,at,algorithm,market,line,outcome) DO NOTHING')
            cursor = con.cursor()
            try:
                cursor.executemany(sql, [tuple(row.get(name) for name in PREDICTION_COLUMNS)
                                         for row in values])
            finally:
                cursor.close()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=2)) as con, con:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA synchronous=FULL')
        con.execute('CREATE TABLE IF NOT EXISTS predictions (version TEXT,match_id TEXT,at TEXT,cutoff_ms REAL,'
                    'algorithm TEXT,market TEXT,line TEXT,outcome TEXT,baseline REAL,x TEXT,probability REAL,'
                    'PRIMARY KEY(version,match_id,at,algorithm,market,line,outcome))')
        if 'predicted_at' not in {r[1] for r in con.execute('PRAGMA table_info(predictions)')}:
            con.execute('ALTER TABLE predictions ADD COLUMN predicted_at TEXT')
        for column in ('decision_received_at_ms', 'quote_received_at_ms'):
            if column not in {r[1] for r in con.execute('PRAGMA table_info(predictions)')}:
                con.execute('ALTER TABLE predictions ADD COLUMN ' + column + ' REAL')
        migrate_basis_columns(con, 'predictions')
        columns = ','.join(PREDICTION_COLUMNS)
        names = ','.join(':' + name for name in PREDICTION_COLUMNS)
        con.executemany('INSERT OR IGNORE INTO predictions (' + columns + ') VALUES (' + names + ')', values)


BASIS_COLUMNS = ('entry_score', 'settlement_basis', 'model_version', 'data_version', 'feature_version')


def migrate_basis_columns(con: sqlite3.Connection, table: str) -> None:
    if table not in ('inputs', 'predictions'):
        raise ValueError('不支持的训练表')
    existing = {r[1] for r in con.execute('PRAGMA table_info(' + table + ')')}
    for name in BASIS_COLUMNS:
        if name not in existing:
            # NULL old values remain quarantined. A migration cannot certify old features.
            con.execute('ALTER TABLE ' + table + ' ADD COLUMN ' + name + ' TEXT')


def labels(con: sqlite3.Connection, mids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    # Select the latest explicitly available revision per match. Never infer
    # availability from settled_at: old repaired rows may have been discovered
    # much later than the event they describe.
    has_revisions = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='label_revisions'").fetchone()
    if has_revisions:
        query = "SELECT match_id,available_at,ft_score,ht_score,revision_id FROM label_revisions "
        query += "WHERE status IN ('won','lost','half_won','half_lost','push','void') "
        if mids is not None:
            query += 'AND match_id IN (' + ','.join('?' for _ in mids) + ') '
        source = con.execute(query + 'ORDER BY revision_order,revision_id', mids or [])
    else:
        # Older projections may not have the indexed column yet. Read the
        # explicit point-in-time timestamp from the immutable payload; never
        # substitute settled_at for repaired legacy records.
        query = ("SELECT match_id,json_extract(payload,'$.label_available_at'),"
            "json_extract(payload,'$.ft_score'),json_extract(payload,'$.ht_score'),decision_id "
            "FROM decisions WHERE competition_type='real' "
            "AND status IN ('won','lost','half_won','half_lost','push','void') "
            "AND json_extract(payload,'$.label_available_at') IS NOT NULL "
            "AND json_extract(payload,'$.label_available_at')!='' "
            "AND json_extract(payload,'$.ft_score') IS NOT NULL ")
        if mids is not None:
            query += 'AND match_id IN (' + ','.join('?' for _ in mids) + ') '
        source = con.execute(query + "ORDER BY json_extract(payload,'$.label_available_at')", mids or [])
    for mid, at, ft, ht, revision in source:
        available = _label_time(at)
        if available is None:
            continue
        try:
            ft_score = _score_of(json.loads(ft) if isinstance(ft, str) else ft)
            ht_score = _score_of(json.loads(ht) if isinstance(ht, str) and ht else ht) if ht else None
        except (TypeError, ValueError):
            continue
        if ft_score is not None:
            previous = result.get(str(mid), {})
            first_at = min(available.isoformat(), previous.get('first_label_available_at', available.isoformat()))
            # The source is revision ordered; corrected payment uses its latest
            # valid row while eligibility uses earliest actual availability.
            result[str(mid)] = {'ft_score': ft_score, 'ht_score': ht_score,
                                'settled_at': available.isoformat(), 'first_label_available_at': first_at,
                                'label_version': revision}
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


def _pg_row(cursor: Any, values: Any) -> dict[str, Any]:
    return {str(description[0]): value for description, value in zip(cursor.description, values)}


def _labels_postgres(connection: Any, *, fetch_size: int) -> dict[str, dict[str, Any]]:
    """Read the latest explicitly available label from the PG projection."""
    result: dict[str, dict[str, Any]] = {}
    cursor = connection.cursor()
    try:
        # Analytics writes append-only label_revisions. model_labels is the
        # compact migration projection. A missing table or failed query must
        # fail closed; retrying inside a PostgreSQL aborted transaction would
        # only hide the real schema/replication problem.
        cursor.execute("SELECT match_id,available_at,ft_score,ht_score,revision_order,revision_id "
                       "FROM model_labels WHERE status IN ('won','lost','half_won','half_lost','push','void') "
                       "UNION ALL SELECT match_id,available_at,ft_score,ht_score,revision_order,revision_id "
                       "FROM label_revisions WHERE status IN ('won','lost','half_won','half_lost','push','void') "
                       "ORDER BY revision_order,revision_id")
        while True:
            batch = cursor.fetchmany(fetch_size)
            if not batch:
                break
            for values in batch:
                row = _pg_row(cursor, values)
                ft = row.get('ft_score')
                ht = row.get('ht_score')
                ft_score = _score_of(json.loads(ft) if isinstance(ft, str) else ft)
                ht_score = (_score_of(json.loads(ht) if isinstance(ht, str) and ht else ht)
                            if ht else None)
                if ft_score is None:
                    continue
                mid = str(row['match_id'])
                if (str(row['available_at']), str(row.get('revision_id') or '')) >= (
                        str(result.get(mid, {}).get('settled_at', '')), ''):
                    result[mid] = {'ft_score': ft_score, 'ht_score': ht_score,
                                   'settled_at': str(row['available_at'])}
    finally:
        cursor.close()
    return result


def _dataset_postgres(projection: ProjectionTarget, *, audit: dict[str, Any] | None,
                      max_per_match: int | None) -> list[dict[str, Any]]:
    """Build a time-safe dataset from the PostgreSQL read-only projection."""
    rows: list[dict[str, Any]] = []
    # The indexed training query audits only version/time/label-qualified
    # inputs, capped per match in production. It cannot certify rejected or
    # waiting archive rows. Keep unaudited totals unknown rather than zero.
    counts: dict[str, Any] = {
        'input_records': None, 'input_matches': None, 'valid_training_decisions': None,
        'waiting_for_label': None, 'quarantined_inputs': None, 'quarantined_ledger': None,
        'ungraded_inputs': None, 'quarantined_missing_time_contract': None,
        'eligible_time_contract': None, 'labelled_time_contract': None,
        'estimated_input_records': None, 'estimated_input_matches': None,
        'counts_estimated': True, 'full_audit_complete': False,
        'counts_scope': 'sampled_qualified_inputs' if max_per_match is not None else 'qualified_inputs',
        'sampled': max_per_match is not None, 'training_sampled': max_per_match is not None,
        'sample_examined_records': 0, 'sample_examined_matches': 0,
        'sample_valid_training_decisions': 0, 'sample_ungraded_inputs': 0,
        'sample_missing_labels': 0,
    }
    buckets: dict[str, list] = {}
    target_cache: dict[tuple[Any, ...], tuple[int, float] | None] = {}
    source_count = 0
    with readonly_snapshot(projection) as con:
        finals = _labels_postgres(con, fetch_size=projection.fetch_size)
        cursor = con.cursor()
        try:
            # ``model_inputs`` is a remote, append-only projection with more
            # than 23M rows. Exact COUNT(*) calls here used to run once for
            # every worker/API/archive process and held a repeatable snapshot
            # open for many minutes. Catalog estimates describe storage size,
            # not data quality; the fitting query below grades the bounded
            # sample and reports its separate, exact counters.
            cursor.execute("SELECT c.reltuples::bigint, s.n_distinct "
                           "FROM pg_class c LEFT JOIN pg_stats s ON s.schemaname=current_schema() "
                           "AND s.tablename='model_inputs' AND s.attname='match_id' "
                           "WHERE c.oid='model_inputs'::regclass")
            estimate_row = cursor.fetchone()
            if estimate_row and estimate_row[0] is not None and estimate_row[0] >= 0:
                estimated_records = int(estimate_row[0])
                counts['estimated_input_records'] = estimated_records
                if (estimate_row[1] is not None and math.isfinite(float(estimate_row[1]))
                        and (float(estimate_row[1]) != 0 or estimated_records == 0)):
                    distinct = float(estimate_row[1])
                    estimated_matches = int(distinct if distinct >= 0 else -distinct * estimated_records)
                    counts['estimated_input_matches'] = min(estimated_records, max(0, estimated_matches))
            version_args = (DATA_VERSION, MODEL_VERSION, FEATURE_VERSION)
            finite = '1.7976931348623157e308'
            time_filter = (
                "cutoff_ms > 0 AND cutoff_ms < " + finite +
                " AND decision_received_at_ms > 0 AND decision_received_at_ms < " + finite +
                " AND decision_received_at_ms <= cutoff_ms"
                " AND quote_received_at_ms > 0 AND quote_received_at_ms < " + finite +
                " AND quote_received_at_ms <= cutoff_ms")
            # LedgerDB writes append-only label_revisions. model_labels is a
            # compact migration projection, so the join must use the latest
            # row from both tables instead of requiring the compact table to
            # be populated first.
            label_source = ("SELECT match_id,available_at,revision_order,revision_id "
                            "FROM model_labels WHERE status IN "
                            "('won','lost','half_won','half_lost','push','void') "
                            "UNION ALL SELECT match_id,available_at,revision_order,revision_id "
                            "FROM label_revisions WHERE status IN "
                            "('won','lost','half_won','half_lost','push','void')")
            latest_labels = ('(SELECT match_id,available_at FROM ('
                             'SELECT match_id,available_at,ROW_NUMBER() OVER '
                             '(PARTITION BY match_id ORDER BY revision_order DESC,revision_id DESC) AS rn '
                             'FROM (' + label_source + ') AS all_labels) AS ranked WHERE rn=1)')
            # Bound the remote result before Python decodes feature JSON. A
            # global ROW_NUMBER over 23M rows is too expensive on a remote PG
            # server, so drive the query from the small latest-label set and
            # use an indexed LATERAL lookup per match. The ordering uses only
            # fields available at decision time; no post-decision quote can
            # enter the training sample.
            source_sql, source_args = _postgres_training_sample_query(
                latest_labels, time_filter, version_args, max_per_match)
            cursor.execute(source_sql, source_args)
            seen_by_match: dict[str, int] = {}
            while True:
                batch = cursor.fetchmany(projection.fetch_size)
                if not batch:
                    break
                for values in batch:
                    raw = _pg_row(cursor, values)
                    mid = str(raw['match_id'])
                    seen_by_match[mid] = seen_by_match.get(mid, 0) + 1
                    counts['sample_examined_records'] += 1
                    final = finals.get(mid)
                    if not final:
                        counts['sample_missing_labels'] += 1
                        continue
                    target_key = (raw.get('market'), raw.get('outcome'), raw.get('line'),
                                  raw.get('settlement_basis'), raw.get('entry_score'),
                                  final['ft_score'], final.get('ht_score'))
                    if target_key not in target_cache:
                        if len(target_cache) >= TARGET_CACHE_LIMIT:
                            target_cache.clear()
                        target_cache[target_key] = target(raw, final)
                    paid = target_cache[target_key]
                    if paid is None:
                        counts['sample_ungraded_inputs'] += 1
                        continue
                    y, weight = paid
                    source_count += 1
                    key = tuple(raw[k] for k in ('match_id', 'at', 'algorithm', 'market', 'line', 'outcome'))
                    item = {'match_id': raw['match_id'], 'at': raw['at'],
                            'settled_at': final['settled_at'], 'x': json.loads(raw['x']), 'y': y,
                            'observation_weight': weight, 'baseline': raw['baseline'],
                            'algorithm': raw['algorithm'], 'market': raw['market'],
                            'line': raw['line'], 'outcome': raw['outcome']}
                    if max_per_match is None:
                        rows.append(item)
                    else:
                        priority = int.from_bytes(hashlib.sha256(json.dumps(key).encode()).digest()[:8], 'big')
                        bucket = buckets.setdefault(mid, [])
                        candidate = (-priority, source_count, item)
                        if len(bucket) < max_per_match:
                            heapq.heappush(bucket, candidate)
                        elif candidate[0] > bucket[0][0]:
                            heapq.heapreplace(bucket, candidate)
                    counts['sample_valid_training_decisions'] += 1
            counts['sample_examined_matches'] = len(seen_by_match)
        finally:
            cursor.close()
        if max_per_match is not None:
            rows = sorted((item[2] for bucket in buckets.values() for item in bucket),
                          key=lambda row: row['at'])
        if audit is not None:
            if max_per_match is None:
                # All qualified inputs were graded, but rows excluded by SQL
                # remain unaudited even in this uncapped diagnostic path.
                counts['valid_training_decisions'] = source_count
                counts['ungraded_inputs'] = counts['sample_ungraded_inputs']
            audit.update(counts, invalid_training_records=None,
                         source_eligible_decisions=source_count if max_per_match is None else None,
                         source_eligible_decisions_scope=counts['counts_scope'],
                         training_max_rows_per_match=max_per_match,
                         sampling_method=('earliest_qualified_inputs_per_match' if max_per_match is not None
                                          else 'all_qualified_inputs'),
                         projection_backend='postgresql')
    return rows


def _pg_scalar(cursor: Any, operation: str, parameters: tuple[Any, ...] = ()) -> int:
    cursor.execute(operation, parameters)
    value = cursor.fetchone()[0]
    return int(value or 0)


def _postgres_training_sample_query(latest_labels: str, time_filter: str,
                                    version_args: tuple[str, str, str],
                                    max_per_match: int | None) -> tuple[str, tuple[Any, ...]]:
    """Build a bounded, label-time-safe PostgreSQL training query."""
    qualified_time = (time_filter.replace('cutoff_ms', 'i.cutoff_ms')
                      .replace('decision_received_at_ms', 'i.decision_received_at_ms')
                      .replace('quote_received_at_ms', 'i.quote_received_at_ms'))
    if max_per_match is None:
        return ('SELECT i.* FROM model_inputs i JOIN ' + latest_labels + ' l '
                'ON l.match_id=i.match_id WHERE i.data_version=%s AND i.model_version=%s '
                'AND i.feature_version=%s AND ' + qualified_time + ' AND l.available_at > i.at',
                version_args)
    sql = ('SELECT i.* FROM ' + latest_labels + ' l CROSS JOIN LATERAL '
           '(SELECT i.* FROM model_inputs i WHERE i.match_id=l.match_id '
           'AND i.data_version=%s AND i.model_version=%s AND i.feature_version=%s AND ' +
           qualified_time + ' AND l.available_at > i.at '
           'ORDER BY i.at,i.algorithm,i.market,i.line,i.outcome LIMIT %s) i')
    return sql, (*version_args, max_per_match)


def dataset(db: Path, inputs_path: Path | None = None, *, audit: dict[str, Any] | None = None,
            max_per_match: int | None = None) -> list[dict[str, Any]]:
    if max_per_match is not None and (isinstance(max_per_match, bool) or not isinstance(max_per_match, int) or max_per_match < 1):
        raise ValueError('每场训练样本上限必须为正整数')
    projection = projection_target_from_env()
    if projection is not None:
        return _dataset_postgres(projection, audit=audit, max_per_match=max_per_match)
    if not db.exists():
        return []
    con = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    rows = []
    counts = {'input_records': 0, 'input_matches': 0, 'valid_training_decisions': 0, 'waiting_for_label': 0,
              'quarantined_inputs': 0, 'quarantined_ledger': 0, 'ungraded_inputs': 0,
              'quarantined_missing_time_contract': 0, 'eligible_time_contract': 0}
    buckets: dict[str, list] = {}
    source_count = 0
    # Most rows in a match share the same score/line/basis.  Settlement is
    # deterministic, so memoizing it removes millions of repeated JSON parses
    # and split-line calculations while preserving the exact target function.
    target_cache: dict[tuple[Any, ...], tuple[int, float] | None] = {}
    match_totals: dict[str, int] = {}
    def collect(row: dict[str, Any], identity: tuple) -> None:
        nonlocal source_count
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
                # Keep the audit counts and training rows on one WAL snapshot
                # while the replay worker continues appending inputs.
                inputs.execute('BEGIN')
                inputs.row_factory = sqlite3.Row
                has_inputs = inputs.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inputs'").fetchone()
                if has_inputs:
                    counts['input_records'] = inputs.execute('SELECT count(*) FROM inputs').fetchone()[0]
                    counts['input_matches'] = inputs.execute('SELECT count(DISTINCT match_id) FROM inputs').fetchone()[0]
                columns = {r[1] for r in inputs.execute('PRAGMA table_info(inputs)')}
                source: Iterable[sqlite3.Row] = ()
                if set(BASIS_COLUMNS) <= columns:
                    counts['quarantined_inputs'] = inputs.execute(
                        "SELECT count(*) FROM inputs WHERE data_version IS NOT ? OR model_version IS NOT ? OR feature_version IS NOT ?",
                        (DATA_VERSION, MODEL_VERSION, FEATURE_VERSION)).fetchone()[0]
                    version_filter = 'data_version=? AND model_version=? AND feature_version=?'
                    version_args = (DATA_VERSION, MODEL_VERSION, FEATURE_VERSION)
                    versioned_rows = inputs.execute(
                        'SELECT count(*) FROM inputs WHERE ' + version_filter, version_args).fetchone()[0]
                    required_clocks = {'cutoff_ms', 'decision_received_at_ms', 'quote_received_at_ms'}
                    if required_clocks <= columns:
                        finite = '1.7976931348623157e308'
                        time_filter = (
                            "typeof(cutoff_ms) IN ('integer','real') AND cutoff_ms>0 AND cutoff_ms<" + finite +
                            " AND typeof(decision_received_at_ms) IN ('integer','real') AND "
                            "decision_received_at_ms>0 AND decision_received_at_ms<" + finite +
                            " AND decision_received_at_ms<=cutoff_ms "
                            "AND typeof(quote_received_at_ms) IN ('integer','real') AND "
                            "quote_received_at_ms>0 AND quote_received_at_ms<" + finite +
                            " AND quote_received_at_ms<=cutoff_ms")
                        counts['eligible_time_contract'] = inputs.execute(
                            'SELECT count(*) FROM inputs WHERE ' + version_filter + ' AND ' + time_filter,
                            version_args).fetchone()[0]
                        counts['quarantined_missing_time_contract'] = (
                            versioned_rows - counts['eligible_time_contract'])

                        # Filter on explicit label availability in SQLite before
                        # decoding feature JSON. The input journal can contain
                        # millions of still-live or not-yet-settled decisions;
                        # Python only needs to grade rows whose result was
                        # available after their decision timestamp.
                        inputs.execute('CREATE TEMP TABLE training_labels '
                            '(match_id TEXT PRIMARY KEY, available_at TEXT NOT NULL)')
                        inputs.executemany('INSERT INTO training_labels(match_id,available_at) VALUES (?,?)',
                                           [(mid, final['settled_at']) for mid, final in finals.items()])
                        qualified_time_filter = (
                            "typeof(i.cutoff_ms) IN ('integer','real') AND i.cutoff_ms>0 AND i.cutoff_ms<" + finite +
                            " AND typeof(i.decision_received_at_ms) IN ('integer','real') AND "
                            "i.decision_received_at_ms>0 AND i.decision_received_at_ms<" + finite +
                            " AND i.decision_received_at_ms<=i.cutoff_ms "
                            "AND typeof(i.quote_received_at_ms) IN ('integer','real') AND "
                            "i.quote_received_at_ms>0 AND i.quote_received_at_ms<" + finite +
                            " AND i.quote_received_at_ms<=i.cutoff_ms")
                        available_filter = (' FROM inputs AS i JOIN training_labels AS l '
                            'ON l.match_id=i.match_id WHERE i.data_version=? AND i.model_version=? '
                            'AND i.feature_version=? AND ' + qualified_time_filter +
                            ' AND l.available_at>i.at')
                        counts['labelled_time_contract'] = inputs.execute(
                            'SELECT count(*)' + available_filter, version_args).fetchone()[0]
                        counts['waiting_for_label'] = (counts['eligible_time_contract'] -
                                                       counts['labelled_time_contract'])
                        # Do not sort millions of rows in a temporary SQLite
                        # result. The final sampled rows are sorted in Python.
                        source = inputs.execute('SELECT i.*' + available_filter, version_args)
                        match_totals = {
                            str(mid): int(total) for mid, total in inputs.execute(
                                'SELECT i.match_id, count(*)' + available_filter +
                                ' GROUP BY i.match_id', version_args)
                        }
                    else:
                        counts['quarantined_missing_time_contract'] = versioned_rows
                else:
                    counts['quarantined_inputs'] = inputs.execute('SELECT count(*) FROM inputs').fetchone()[0]
                seen_by_match: dict[str, int] = {}
                for r in source:
                    mid = str(r['match_id'])
                    seen_by_match[mid] = seen_by_match.get(mid, 0) + 1
                    final = finals.get(mid)
                    if not final:
                        continue
                    # Grade every row for honest quality counters, but defer
                    # feature JSON decoding until an evenly spaced sample is
                    # selected. This keeps the 2 GiB worker below its address
                    # space limit even with millions of archived inputs.
                    target_key = (r['market'], r['outcome'], r['line'],
                                  r['settlement_basis'], r['entry_score'],
                                  final['ft_score'], final.get('ht_score'))
                    if target_key not in target_cache:
                        if len(target_cache) >= TARGET_CACHE_LIMIT:
                            # Avoid trading the previous MemoryError for an
                            # unbounded cache when every decision has a unique
                            # score snapshot. Repeated rows still benefit from
                            # the cache within each bounded window.
                            target_cache.clear()
                        target_cache[target_key] = target({
                            'entry_score': r['entry_score'], 'market': r['market'],
                            'outcome': r['outcome'], 'line': r['line'],
                            'settlement_basis': r['settlement_basis']}, final)
                    paid = target_cache[target_key]
                    if paid is None:
                        counts['ungraded_inputs'] += 1
                        continue
                    y, weight = paid
                    source_count += 1
                    total = match_totals.get(mid, 0)
                    if (max_per_match is not None and total > max_per_match and
                            ((seen_by_match[mid] - 1) * max_per_match) % total >= max_per_match):
                        counts['valid_training_decisions'] += 1
                        continue
                    key = tuple(r[k] for k in ('match_id', 'at', 'algorithm', 'market', 'line', 'outcome'))
                    collect({'match_id': r['match_id'], 'at': r['at'], 'settled_at': final['settled_at'],
                             'x': json.loads(r['x']), 'y': y, 'observation_weight': weight,
                             'baseline': r['baseline'], 'algorithm': r['algorithm'],
                             'market': r['market'], 'line': r['line'], 'outcome': r['outcome']}, key)
                    counts['valid_training_decisions'] += 1
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
                         training_max_rows_per_match=max_per_match,
                         counts_scope='full_inputs', counts_estimated=False,
                         full_audit_complete=True, sampled=False,
                         training_sampled=max_per_match is not None,
                         source_eligible_decisions_scope='full_inputs',
                         sampling_method=('evenly_spaced_then_min_hash_per_match' if max_per_match is not None
                                          else 'all_qualified_inputs'),
                         projection_backend='sqlite')
        if max_per_match is not None:
            rows = sorted((item[2] for bucket in buckets.values() for item in bucket), key=lambda r:r['at'])
        return rows
    finally:
        con.close()


SHADOW_WINDOW_LIMIT = 20_000
SHADOW_FETCH_LIMIT = 2_000
SHADOW_IDENTITY = ('at', 'match_id', 'algorithm', 'market', 'line', 'outcome')
SHADOW_CURSOR = ('predicted_at', *SHADOW_IDENTITY)


def _prediction_identity(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(row[key]) for key in SHADOW_IDENTITY)


def _shadow_label_subset(connection: Any, mids: list[str]) -> dict[str, dict[str, Any]]:
    """Only resolve labels for the bounded prediction window, never inputs."""
    if not mids:
        return {}
    cursor = connection.cursor()
    try:
        placeholders = ','.join(['%s'] * len(mids))
        # First terminal score controls whether a prediction was prospective;
        # the newest valid revision supplies the corrected settlement target.
        # Filtering happens before both windows, so invalid/pending scores
        # cannot move first availability or hide a valid earlier revision.
        score_pattern = (r'^[[:space:]]*\[[[:space:]]*(100|[0-9]{1,2})[[:space:]]*,'
                         r'[[:space:]]*(100|[0-9]{1,2})[[:space:]]*\][[:space:]]*$')
        cursor.execute('SELECT match_id,available_at,ft_score,ht_score,revision_id,first_label_available_at FROM ('
            'SELECT match_id,available_at,ft_score,ht_score,revision_id,FIRST_VALUE(available_at) OVER '
            '(PARTITION BY match_id ORDER BY available_time,revision_id) first_label_available_at,ROW_NUMBER() OVER '
            '(PARTITION BY match_id ORDER BY revision_order DESC,revision_id DESC) rn FROM ('
            'SELECT labels.*,CASE WHEN pg_input_is_valid(available_at,\'timestamp with time zone\') '
            "AND available_at ~ %s THEN available_at::timestamptz ELSE NULL END available_time FROM ("
            'SELECT match_id,available_at,ft_score,ht_score,status,revision_order,revision_id '
            'FROM model_labels WHERE match_id IN (' + placeholders + ') UNION ALL '
            'SELECT match_id,available_at,ft_score,ht_score,status,revision_order,revision_id '
            'FROM label_revisions WHERE match_id IN (' + placeholders + ')) labels) timed '
            "WHERE status IN ('won','lost','half_won','half_lost','push','void') "
            "AND available_time IS NOT NULL AND ft_score ~ %s) ranked WHERE rn=1",
            (r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$', *mids, *mids, score_pattern))
        finals = {}
        for values in cursor.fetchall():
            row = _pg_row(cursor, values)
            ft = _score_of(json.loads(row['ft_score']) if isinstance(row['ft_score'], str) else row['ft_score'])
            ht = row.get('ht_score')
            try:
                ht = _score_of(json.loads(ht) if isinstance(ht, str) else ht) if ht else None
            except (TypeError, ValueError):
                ht = None
            if ft is not None:
                finals[str(row['match_id'])] = {'ft_score': ft, 'ht_score': ht,
                    'settled_at': str(row['available_at']),
                    'first_label_available_at': str(row['first_label_available_at']),
                    'label_version': str(row['revision_id'])}
        return finals
    finally:
        cursor.close()


def prospective_prediction_rows(db: Path, rows: list[dict[str, Any]], *,
                                 projection_target: ProjectionTarget | None = None) -> list[dict[str, Any]]:
    """Do not persist settled replay as prospective predictions.

    Labels are resolved only for the submitted match ids. A failed label read
    fails closed and leaves the worker's pending batch retryable.
    """
    if not rows:
        return []
    mids = sorted({str(row['match_id']) for row in rows})
    target = projection_target or projection_target_from_env()
    finals = {}
    if target is not None:
        with readonly_snapshot(target) as con:
            for offset in range(0, len(mids), SHADOW_FETCH_LIMIT):
                finals.update(_shadow_label_subset(con, mids[offset:offset+SHADOW_FETCH_LIMIT]))
    elif db.exists():
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as con:
            for offset in range(0, len(mids), 500):
                finals.update(labels(con, mids[offset:offset+500]))
    return _without_known_settled_predictions(rows, finals)


def _without_known_settled_predictions(rows: list[dict[str, Any]],
                                       finals: Mapping[str, Any]) -> list[dict[str, Any]]:
    from datetime import datetime
    result = []
    for row in rows:
        final = finals.get(str(row['match_id']))
        if not final:
            result.append(row)
            continue
        try:
            first = datetime.fromisoformat(final.get('first_label_available_at') or final['settled_at'])
            predicted = datetime.fromisoformat(str(row['predicted_at']))
            if first.tzinfo is not None and predicted.tzinfo is not None and first > predicted:
                result.append(row)
        except (KeyError, TypeError, ValueError):
            continue
    return result


def _shadow_metrics(rows: list[dict[str, Any]], finals: Mapping[str, Any],
                    model: Mapping[str, Any]) -> dict[str, Any]:
    from datetime import datetime, timezone
    from core.model_validation import evaluate, coherent_probabilities
    valid, probabilities, baseline = [], [], []
    pending = ungraded = excluded = settled = 0
    latencies = []
    for raw in rows:
        try:
            if raw.get('version') != model['version'] or any(raw.get(key) != value for key,value in (
                    ('data_version', DATA_VERSION), ('model_version', MODEL_VERSION), ('feature_version', FEATURE_VERSION))):
                raise ValueError('预测版本不匹配')
            at = datetime.fromisoformat(str(raw['at']))
            predicted = datetime.fromisoformat(str(raw.get('predicted_at') or ''))
            available = datetime.fromisoformat(str(model.get('available_at') or model['updated_at']))
            cutoff = float(raw['cutoff_ms'])
            if (any(t.tzinfo is None for t in (at, predicted, available)) or
                    not math.isfinite(cutoff) or cutoff <= 0 or cutoff > at.timestamp()*1000 or
                    at < available or available.timestamp() * 1000 > cutoff or predicted < at or
                    any(not isinstance(raw.get(k), (float, int)) or isinstance(raw.get(k), bool) or
                        not math.isfinite(raw[k]) or not 0 < raw[k] <= cutoff
                        for k in ('decision_received_at_ms', 'quote_received_at_ms'))):
                excluded += 1
                continue
            p, b = float(raw['probability']), float(raw['baseline'])
            if not all(math.isfinite(v) and 0 <= v <= 1 for v in (p, b)):
                raise ValueError('无效概率')
            final = finals.get(str(raw['match_id']))
            if not final:
                latencies.append((predicted-at).total_seconds()*1000)
                pending += 1
                continue
            first_label = datetime.fromisoformat(final.get('first_label_available_at') or final['settled_at'])
            if first_label.tzinfo is None or first_label <= predicted:
                excluded += 1
                continue
            latencies.append((predicted-at).total_seconds()*1000)
            settled += 1
            paid = target(raw, final)
            if paid is None:
                ungraded += 1
                continue
            y, weight = paid
            valid.append({**raw, 'y': y, 'observation_weight': weight,
                          'label_version': final.get('label_version'),
                          'first_label_available_at': first_label.isoformat()})
            probabilities.append(p)
            baseline.append(b)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            excluded += 1
    empty: dict[str, Any] = {'accuracy': None, 'binary_accuracy': None, 'had_top1_accuracy': None,
             'had_top1_matches': 0, 'matches': 0, 'decisions': 0, 'brier': None,
             'log_loss': None, 'asian_payment_accuracy': None, 'asian_selected_contracts': 0}
    scores = []
    for ps in (probabilities, baseline):
        score = evaluate(valid, ps) if valid else dict(empty)
        coherent = coherent_probabilities(valid, ps)
        groups: dict[tuple, list[int]] = {}
        for i, row in enumerate(valid):
            if str(row['market']).startswith(('OU', 'AH')):
                groups.setdefault((row['match_id'], row['at'], row['algorithm'], row['market'], row['line']), []).append(i)
        selected = [max(indices, key=lambda i: coherent[i]) for indices in groups.values()
                    if len(indices) == 2 and len({valid[i]['outcome'] for i in indices}) == 2]
        by_match: dict[str, list[tuple[int, float]]] = {}
        for i in selected:
            by_match.setdefault(str(valid[i]['match_id']), []).append((valid[i]['y'], valid[i]['observation_weight']))
        score['asian_payment_accuracy'] = (sum(sum(y*w for y,w in pairs)/sum(w for _,w in pairs)
            for pairs in by_match.values())/len(by_match)) if by_match else None
        score['asian_selected_contracts'] = len(selected)
        score['asian_payment_basis'] = '每个盘口时点选择最高概率方向；半赢半输按0.5本金、比赛等权；走盘不计有效输赢分母'
        scores.append(score)
    latencies.sort()
    settled_mids = {str(row['match_id']) for row in valid}
    return {'version': model['version'], 'data_version': DATA_VERSION,
            'artifact_kind': model.get('artifact_kind', 'active'),
            'status': 'ready' if valid else 'waiting_for_settlement' if pending else 'no_predictions',
            'reason': '' if valid else '影子预测等待确认赛果' if pending else
                '已结算预测暂无有效输赢样本（走盘或结算口径不可核验）' if ungraded else '尚无可结算的前瞻影子预测',
            'accuracy_basis': '按比赛等权；保留同场多个盘口/时点/算法；二元正确率为各候选概率>=0.5与支付标签比较，并非推荐下注胜率；HAD另报完整三向argmax命中',
            'updated_at': datetime.now(timezone.utc).isoformat(),
            'window': {'from': min((r['at'] for r in rows), default=None),
                       'to': max((r['at'] for r in rows), default=None),
                       'max_predictions': SHADOW_WINDOW_LIMIT, 'scope': 'recent_versioned_predictions'},
            'counts': {'predictions': len(rows), 'settled_predictions': settled,
                       'pending_predictions': pending, 'ungraded_predictions': ungraded,
                       'excluded_predictions': excluded, 'settled_matches': len({r['match_id'] for r in valid})},
            'model': scores[0], 'baseline': scores[1],
            'labels': {str(mid): {'version': final.get('label_version'),
                                  'first_available_at': final.get('first_label_available_at') or final['settled_at'],
                                  'latest_available_at': final['settled_at']}
                       for mid, final in finals.items() if str(mid) in settled_mids},
            'latency': {f'p{percentile}_ms': latencies[min(len(latencies)-1, int((len(latencies)-1)*percentile/100))]
                        if latencies else None for percentile in (50,95,99)}}


def _prospective_metrics_postgres(projection: ProjectionTarget, model: Mapping[str, Any], *,
                                  state: dict[str, Any] | None = None) -> dict[str, Any]:
    if not compatible_model(model):
        return {}
    state = state if state is not None else {}
    if state.get('version') != model['version']:
        state.clear()
        state.update(version=model['version'], rows=[], cursor=None)
    columns = ','.join(SHADOW_CURSOR)
    with readonly_snapshot(projection) as con:
        cursor = con.cursor()
        try:
            query = ('SELECT * FROM model_predictions WHERE version=%s AND data_version=%s '
                     'AND model_version=%s AND feature_version=%s AND predicted_at IS NOT NULL')
            args: list[Any] = [model['version'], DATA_VERSION, MODEL_VERSION, FEATURE_VERSION]
            if state.get('cursor'):
                query += ' AND (' + columns + ') > (' + ','.join(['%s']*len(SHADOW_CURSOR)) + ')'
                args.extend(state['cursor'])
            query += ' ORDER BY ' + columns + ' LIMIT %s'
            args.append(SHADOW_FETCH_LIMIT)
            cursor.execute(query, tuple(args))
            batch = [_pg_row(cursor, values) for values in cursor.fetchall()]
        finally:
            cursor.close()
        if batch:
            state['cursor'] = [str(batch[-1][key]) for key in SHADOW_CURSOR]
        combined = (list(state.get('rows', [])) +
                    [{key:value for key,value in row.items() if key != 'x'} for row in batch])
        finals = _shadow_label_subset(con, sorted({str(r['match_id']) for r in combined}))
        prospective = _without_known_settled_predictions(combined, finals)
        state['excluded_replay_predictions'] = (state.get('excluded_replay_predictions', 0) +
                                               len(combined)-len(prospective))
        state['rows'] = prospective[-SHADOW_WINDOW_LIMIT:]
    result = _shadow_metrics(state['rows'], finals, model)
    result['cursor'] = {'batch_records': len(batch), 'has_more': len(batch) == SHADOW_FETCH_LIMIT}
    result['counts']['excluded_replay_predictions'] = state['excluded_replay_predictions']
    result['counts']['excluded_replay_scope'] = 'cursor_history_not_window'
    return result


def prospective_metrics(db: Path, path: Path, model: Mapping[str, Any], *,
                        projection_target: ProjectionTarget | None = None,
                        state: dict[str, Any] | None = None) -> dict[str, Any]:
    projection_target = projection_target or projection_target_from_env()
    if projection_target is not None:
        return _prospective_metrics_postgres(projection_target, model, state=state)
    if not compatible_model(model):
        return {}
    finals = {}
    if db.exists():
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as con:
            finals = labels(con)
    rows: list[dict[str, Any]] = []
    if path.exists():
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as con:
            con.row_factory = sqlite3.Row
            columns = {r[1] for r in con.execute('PRAGMA table_info(predictions)')}
            if set(BASIS_COLUMNS) <= columns and 'predicted_at' in columns:
                rows = [dict(raw) for raw in con.execute(
                    'SELECT * FROM predictions WHERE version=? AND data_version=? AND model_version=? '
                    'AND feature_version=? ORDER BY at DESC,match_id DESC,algorithm DESC,market DESC,line DESC,outcome DESC LIMIT ?',
                    (model['version'], DATA_VERSION, MODEL_VERSION, FEATURE_VERSION, SHADOW_WINDOW_LIMIT))]
    return _shadow_metrics(rows, finals, model)
