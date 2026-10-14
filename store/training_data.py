"""Immutable model inputs; every feature belongs to one pre-decision snapshot."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone

import hashlib
import json
from itertools import groupby
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any, ContextManager, Iterable, Mapping

from core.learned_model import DATA_VERSION, compatible_model, features
from core.decision_features import FEATURE_VERSION, verified_context, visible_history
from core.live_model import MODEL_VERSION
from core.settlement import _score_of, pnl_for, settle_with_basis
from store.training_projection import (
    ProjectionTarget,
    SHADOW_LABEL_SCORE_PATTERN,
    prepare_postgres_schema,
    persistent_writer,
    projection_target_from_env,
    readonly_snapshot,
    writable_connection,
)

GRADED = ('won', 'lost', 'half_won', 'half_lost')
TARGET_CACHE_LIMIT = 100_000


def _complete_market_curve(rows: Iterable[Mapping[str, Any]]) -> bool:
    members = list(rows)
    if not members:
        return False
    market = str(members[0]['market'])
    expected = ({'home','draw','away'} if market.startswith('HAD') else
                {'over','under'} if market.startswith('OU') else
                {'home','away'} if market.startswith('AH') else set())
    lines: dict[str,list[Mapping[str,Any]]] = {}
    for member in members:
        if str(member['market']) != market:
            return False
        lines.setdefault(str(member['line']),[]).append(member)
    return bool(expected and all(len(group)==len(expected) and
        {member['outcome'] for member in group}==expected for group in lines.values()))


def _curve_complete_sql(table: str) -> str:
    if table not in ('inputs','model_inputs'):
        raise ValueError('不支持的训练表')
    count = "(CASE WHEN i.market LIKE 'HAD%' THEN 3 WHEN i.market LIKE 'OU%' OR i.market LIKE 'AH%' THEN 2 ELSE 0 END)"
    valid = ("((i.market LIKE 'HAD%' AND j.outcome IN ('home','draw','away')) OR "
             "(i.market LIKE 'OU%' AND j.outcome IN ('over','under')) OR "
             "(i.market LIKE 'AH%' AND j.outcome IN ('home','away')))")
    return (count+'>0 AND NOT EXISTS (SELECT j.line FROM '+table+' j '
        'WHERE j.match_id=i.match_id AND j.at=i.at AND j.algorithm=i.algorithm AND j.market=i.market '
        'AND j.data_version=i.data_version AND j.model_version=i.model_version AND j.feature_version=i.feature_version '
        'GROUP BY j.line HAVING COUNT(*)<>'+count+' OR '
        'SUM(CASE WHEN '+valid+' THEN 1 ELSE 0 END)<>'+count+')')


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
    boundary, cooldown = row.get('score_state_boundary_ms'), row.get('score_change_cooldown_s')
    if (row.get('state_qualified') is not True or
            isinstance(boundary,bool) or not isinstance(boundary,(int,float)) or not math.isfinite(boundary) or boundary <= 0 or
            isinstance(cooldown,bool) or not isinstance(cooldown,(int,float)) or not math.isfinite(cooldown) or cooldown < 0):
        return []
    quote_after = boundary + cooldown * 1000
    if quote_after > cutoff:
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
    qualified_evaluations = []
    for evaluation in evaluations:
        curves: dict[str,list[dict[str,Any]]] = {}
        for candidate in evaluation.get('candidates',[]):
            curves.setdefault(str(candidate['market']),[]).append(candidate)
        qualified = []
        for curve in curves.values():
            if (_complete_market_curve(curve) and all(
                not candidate.get('research_only') and candidate.get('decision_status')=='settleable' and
                all(isinstance(candidate.get(key),(int,float)) and not isinstance(candidate.get(key),bool)
                    and math.isfinite(candidate[key]) and quote_after<=candidate[key]<=cutoff
                    for key in ('ts_ms','quote_received_at_ms')) for candidate in curve)):
                qualified.extend(curve)
        qualified_evaluations.append({**evaluation,'candidates':qualified})
    evaluations = qualified_evaluations
    peers: dict[tuple[str, str, str], dict[str, float]] = {}
    for evaluation in evaluations:
        for candidate in evaluation.get('candidates', []):
            key = (candidate['market'], str(candidate['line']), candidate['outcome'])
            peers.setdefault(key, {})[evaluation['algorithm']] = candidate['p_model']
    for evaluation in evaluations:
        for candidate in evaluation.get('candidates', []):
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
                           'odds': candidate.get('odds'),
                           'entry_score': json.dumps(row['score']), 'settlement_basis': candidate.get('settlement_basis', 'unknown'),
                           'model_version': MODEL_VERSION, 'data_version': DATA_VERSION,
                           'feature_version': FEATURE_VERSION})
    built: dict[tuple[str,str],list[dict[str,Any]]] = {}
    for item in inputs:
        built.setdefault((item['algorithm'],item['market']),[]).append(item)
    return [item for curve in built.values() if _complete_market_curve(curve) for item in curve]


def _normalise_input_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Repair/test callers may provide a v3 vector without repeating the schema
    # marker; normalize it once at the storage boundary.
    return [{**row, 'feature_version': row.get('feature_version', FEATURE_VERSION),
             'odds': row.get('odds'),
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
        for name in ('decision_received_at_ms', 'quote_received_at_ms', 'odds'):
            existing = {r[1] for r in con.execute('PRAGMA table_info(inputs)')}
            if name not in existing:
                con.execute('ALTER TABLE inputs ADD COLUMN ' + name + ' REAL')
        columns = ','.join(INPUT_COLUMNS)
        con.executemany(('INSERT OR REPLACE' if repair else 'INSERT OR IGNORE') + ' INTO inputs (' + columns + ') VALUES (' +
                        ','.join(':' + name for name in columns.split(',')) + ')', rows)


INPUT_COLUMNS = ('match_id', 'at', 'cutoff_ms', 'algorithm', 'market', 'line', 'outcome',
                 'baseline', 'x', 'decision_received_at_ms', 'quote_received_at_ms',
                 'entry_score', 'settlement_basis', 'model_version', 'data_version',
                 'feature_version','odds')

POSTGRES_INSERT_BATCH_ROWS = 256
POSTGRES_SAMPLE_TIME_POINTS = 8
POSTGRES_SAMPLE_MATCH_BATCH = 64


def _insert_postgres_rows(cursor: Any, table: str, columns: tuple[str,...],
                          rows: list[tuple[Any,...]], conflict: str) -> None:
    """One bounded SQL roundtrip per batch, including psycopg2 deployments."""
    if table not in ('model_inputs','model_predictions'):
        raise ValueError('不支持的模型投影表')
    placeholders = '('+','.join('%s' for _ in columns)+')'
    for offset in range(0,len(rows),POSTGRES_INSERT_BATCH_ROWS):
        batch = rows[offset:offset+POSTGRES_INSERT_BATCH_ROWS]
        sql = ('INSERT INTO '+table+' ('+','.join(columns)+') VALUES '+
               ','.join(placeholders for _ in batch)+' ON CONFLICT '+conflict)
        cursor.execute(sql,tuple(value for row in batch for value in row))


def _write_inputs_postgres(rows: list[dict[str, Any]], *, repair: bool,
                           target: ProjectionTarget) -> None:
    if target.backend != 'postgresql':
        raise ValueError('训练输入写入目标必须是 PostgreSQL')
    prepare_postgres_schema(target)
    with writable_connection(target) as con:
        placeholders = ','.join('%s' for _ in INPUT_COLUMNS)
        columns = ','.join(INPUT_COLUMNS)
        action = ('DO UPDATE SET ' + ','.join(f'{name}=EXCLUDED.{name}' for name in INPUT_COLUMNS[1:])
                  if repair else 'DO NOTHING')
        sql = (f'INSERT INTO model_inputs ({columns}) VALUES ({placeholders}) '
               f'ON CONFLICT (match_id,at,algorithm,market,line,outcome) {action}')
        cursor = con.cursor()
        try:
            values = [tuple(row.get(name) for name in INPUT_COLUMNS) for row in rows]
            if repair:
                cursor.executemany(sql, values)
            else:
                _insert_postgres_rows(cursor,'model_inputs',INPUT_COLUMNS,values,
                                      '(match_id,at,algorithm,market,line,outcome) DO NOTHING')
        finally:
            cursor.close()


PREDICTION_COLUMNS = ('version', 'match_id', 'at', 'cutoff_ms', 'algorithm', 'market', 'line',
                      'outcome', 'baseline', 'x', 'probability', 'predicted_at', 'entry_score',
                      'settlement_basis', 'model_version', 'data_version', 'feature_version',
                      'decision_received_at_ms', 'quote_received_at_ms','odds')


def write_predictions(path: Path, rows: Iterable[Mapping[str, Any]], *,
                      target: ProjectionTarget | None = None) -> None:
    """Persist shadow predictions to PostgreSQL or the legacy SQLite file."""
    values = [{**row,'odds':row.get('odds')} for row in rows]
    if not values:
        return
    target = target or projection_target_from_env()
    if target is not None:
        if target.backend != 'postgresql':
            raise ValueError('训练预测写入目标必须是 PostgreSQL')
        prepare_postgres_schema(target)
        with writable_connection(target) as con:
            cursor = con.cursor()
            try:
                _insert_postgres_rows(cursor,'model_predictions',PREDICTION_COLUMNS,
                    [tuple(row.get(name) for name in PREDICTION_COLUMNS) for row in values],
                    '(version,match_id,at,algorithm,market,line,outcome) DO NOTHING')
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
        for column in ('decision_received_at_ms', 'quote_received_at_ms', 'odds'):
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


def _settlement_status(row: Mapping[str, Any], final: Mapping[str, Any]) -> str:
    entry = row.get('entry_score')
    if isinstance(entry, str):
        try:
            entry = json.loads(entry)
        except ValueError:
            return 'void'
    expected = ('remaining_score' if str(row['market']).startswith('AH') else
                'half_score_required' if str(row['market']).endswith('_1H') else 'full_score')
    if row.get('settlement_basis') != expected:
        return 'void'
    status, _ = settle_with_basis(row['market'], row['outcome'], row['line'], final['ft_score'],
                                 final.get('ht_score'), settlement_basis=expected, entry_score=entry)
    return status


def target(row: Mapping[str, Any], final: Mapping[str, Any]) -> tuple[int, float] | None:
    status = _settlement_status(row, final)
    if status not in GRADED:
        return None
    return int(status in ('won', 'half_won')), .5 if status.startswith('half_') else 1.0


def _classification_fields(paid: tuple[int, float] | None) -> dict[str, Any]:
    """Unlabelled outcomes remain prediction context, never synthetic losses."""
    return {'y': paid[0] if paid is not None else None,
            'observation_weight': paid[1] if paid is not None else 0.0,
            'graded': paid is not None}


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
        cursor.execute("SELECT DISTINCT ON (match_id) match_id,available_at,ft_score,ht_score,revision_order,revision_id "
                       "FROM (SELECT match_id,available_at,ft_score,ht_score,revision_order,revision_id "
                       "FROM model_labels WHERE status IN ('won','lost','half_won','half_lost','push','void') "
                       "UNION ALL SELECT match_id,available_at,ft_score,ht_score,revision_order,revision_id "
                       "FROM label_revisions WHERE status IN ('won','lost','half_won','half_lost','push','void')) labels "
                       "ORDER BY match_id,revision_order DESC,revision_id DESC")
        while True:
            batch = cursor.fetchmany(fetch_size)
            if not batch:
                break
            for values in batch:
                row = _pg_row(cursor, values)
                ft = row.get('ft_score')
                ht = row.get('ht_score')
                try:
                    ft_score = _score_of(json.loads(ft) if isinstance(ft, str) else ft)
                    ht_score = (_score_of(json.loads(ht) if isinstance(ht, str) and ht else ht)
                                if ht else None)
                except (ValueError,TypeError):
                    continue
                available = _label_time(row['available_at'])
                if ft_score is None or available is None:
                    continue
                mid = str(row['match_id'])
                result[mid] = {'ft_score': ft_score, 'ht_score': ht_score,
                               'settled_at': available.isoformat(),
                               'label_version':str(row.get('revision_id') or '')}
    finally:
        cursor.close()
    return result


def _dataset_postgres(projection: ProjectionTarget, *, audit: dict[str, Any] | None,
                      max_per_match: int | None,
                      cohort_labels: Mapping[str, Any] | None = None,
                      input_cutoff: str | None = None) -> list[dict[str, Any]]:
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
        'sample_incomplete_outcome_curve_inputs':0,
    }
    target_cache: dict[tuple[Any, ...], tuple[int, float] | None] = {}
    source_count = 0
    with readonly_snapshot(projection) as con:
        finals = (dict(cohort_labels) if cohort_labels is not None else
                  _labels_postgres(con, fetch_size=projection.fetch_size))
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
            if input_cutoff is not None:
                cutoff_at = _label_time(input_cutoff)
                if cutoff_at is None:
                    raise ValueError('冻结训练输入截点必须是带时区的时间')
                # Validated ISO timestamp is not caller-controlled SQL.
                time_filter += " AND at<='"+cutoff_at.isoformat()+"'"
            # Reuse the same latest revisions used to grade rows. Sorting the
            # append-only revision log again costs another large remote scan
            # and could select a different revision from the Python grader.
            latest_labels = ('(SELECT match_id,available_at FROM '
                             'unnest(%s::text[],%s::text[]) AS labels(match_id,available_at))')
            # Bound the remote result before Python decodes feature JSON. A
            # global ROW_NUMBER over 23M rows is too expensive on a remote PG
            # server, so drive the query from the small latest-label set and
            # use an indexed LATERAL lookup per match. The ordering uses only
            # fields available at decision time; no post-decision quote can
            # enter the training sample.
            source_sql, source_args = _postgres_training_sample_query(
                latest_labels, time_filter, version_args, max_per_match)
            seen_by_match: dict[str, int] = {}
            sampled_buckets: set[tuple[str, int]] = set()
            empty_buckets: set[tuple[str, int]] = set()
            empty_curves: set[tuple[str, int]] = set()
            no_input_matches: set[str] = set()
            match_ids = sorted(finals)
            batch_seconds = []
            cursor.execute('SET LOCAL jit=off')
            for offset in range(0, len(match_ids), POSTGRES_SAMPLE_MATCH_BATCH):
                label_batch = match_ids[offset:offset + POSTGRES_SAMPLE_MATCH_BATCH]
                label_args = (label_batch, [finals[mid]['settled_at'] for mid in label_batch])
                batch_started = time.monotonic()
                cursor.execute(source_sql, (*label_args, *source_args))
                while True:
                    batch = cursor.fetchmany(projection.fetch_size)
                    if not batch:
                        break
                    for values in batch:
                        raw = _pg_row(cursor, values)
                        if max_per_match is not None and '_sample_label_match' in raw:
                            bucket_key = (str(raw['_sample_label_match']), int(raw['_sample_bucket']))
                            sampled_buckets.add(bucket_key)
                            if raw.get('_sample_first_at') is None:
                                no_input_matches.add(bucket_key[0])
                            elif raw.get('_sample_at') is None:
                                empty_buckets.add(bucket_key)
                            elif raw.get('match_id') is None:
                                empty_curves.add(bucket_key)
                            if raw.get('match_id') is None:
                                continue
                        mid = str(raw['match_id'])
                        seen_by_match[mid] = seen_by_match.get(mid, 0) + 1
                        counts['sample_examined_records'] += 1
                        if raw.get('_curve_complete') is False:
                            counts['sample_incomplete_outcome_curve_inputs'] += 1
                            continue
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
                        else:
                            source_count += 1
                        item = {'match_id': raw['match_id'], 'at': raw['at'],
                                'settled_at': final['settled_at'], 'x': json.loads(raw['x']),
                                **_classification_fields(paid), 'baseline': raw['baseline'],
                                'algorithm': raw['algorithm'], 'market': raw['market'],
                                'line': raw['line'], 'outcome': raw['outcome']}
                        # SQL caps complete contract groups. A second row-level
                        # reservoir here would split those groups again.
                        rows.append(item)
                        counts['sample_valid_training_decisions'] += int(paid is not None)
                batch_seconds.append(time.monotonic() - batch_started)
            counts['sample_examined_matches'] = len(seen_by_match)
            counts.update(sample_label_matches=len(finals),
                          prediction_context_records=len(rows),
                          prediction_context_matches=len({str(r['match_id']) for r in rows}),
                          sample_valid_training_matches=len({str(r['match_id']) for r in rows if r['graded']}),
                          sample_query_batches=len(batch_seconds),
                          sample_query_match_batch=POSTGRES_SAMPLE_MATCH_BATCH,
                          sample_query_seconds=sum(batch_seconds),
                          sample_max_query_batch_seconds=max(batch_seconds, default=0.0),
                          sample_label_matches_without_inputs=len(no_input_matches),
                          sample_label_matches_without_rows=len(set(finals) - set(seen_by_match)),
                          sample_time_buckets=len(sampled_buckets),
                          sample_empty_time_buckets=len(empty_buckets),
                          sample_time_buckets_without_valid_curves=len(empty_curves),
                          sample_max_rows_per_match=max(seen_by_match.values(), default=0),
                          sample_mean_rows_per_nonempty_match=(sum(seen_by_match.values()) / len(seen_by_match)
                                                              if seen_by_match else 0.0))
        finally:
            cursor.close()
        rows.sort(key=lambda row: (row['at'], row['match_id'], row['algorithm'],
                                  row['market'], row['line'], row['outcome']))
        if audit is not None:
            if max_per_match is None:
                # All qualified inputs were graded, but rows excluded by SQL
                # remain unaudited even in this uncapped diagnostic path.
                counts['valid_training_decisions'] = source_count
                counts['ungraded_inputs'] = counts['sample_ungraded_inputs']
                counts['quarantined_incomplete_outcome_curve_inputs'] = counts['sample_incomplete_outcome_curve_inputs']
            audit.update(counts, invalid_training_records=None,
                         source_eligible_decisions=source_count if max_per_match is None else None,
                         source_eligible_decisions_scope=counts['counts_scope'],
                         training_max_rows_per_match=max_per_match,
                         sampling_cap_basis='soft row target; preserve every line/outcome in selected market curves',
                         sampling_row_bound=None,
                         sampling_time_axis='version-qualified pre-settlement decision times; complete curve clock gates applied afterwards',
                         sampling_unit='match_id,at,algorithm,market',
                         sampling_method=('time_stratified_market_curves_per_match' if max_per_match is not None
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
        return ('SELECT i.*,('+_curve_complete_sql('model_inputs')+') AS _curve_complete FROM model_inputs i JOIN ' + latest_labels + ' l '
                'ON l.match_id=i.match_id WHERE i.data_version=%s AND i.model_version=%s '
                'AND i.feature_version=%s AND ' + qualified_time + ' AND l.available_at > i.at',
                version_args)
    # The old PG path selected the earliest rows. Ranking every row per match
    # spreads the sample correctly but scans too much of the remote projection.
    # Find the first/last qualified timestamps through the existing index,
    # divide that observed range into a small fixed number of time buckets,
    # and sample a bounded set of rows from one quote snapshot in each bucket.
    # Bucket boundaries use decision timestamps only; result labels only gate
    # eligibility and never affect the selected point.
    # The version/match/time index covers this decision-time axis. Applying
    # clock predicates here forces a remote heap lookup per candidate point.
    # Every selected curve below still must satisfy all clock predicates.
    eligible_axis = ('i.match_id=l.match_id AND i.data_version=%s AND i.model_version=%s '
                     'AND i.feature_version=%s AND l.available_at > i.at')
    if ' AND at<=' in time_filter:
        eligible_axis += time_filter[time_filter.rindex(' AND at<='):].replace(' at<=',' i.at<=')
    endpoint = ('SELECT i.at FROM model_inputs i WHERE ' + eligible_axis +
                ' ORDER BY i.at {direction} LIMIT 1')
    points = min(POSTGRES_SAMPLE_TIME_POINTS, max_per_match)
    rows_per_point, extra_rows = divmod(max_per_match, points)
    lower_epoch = ("extract(epoch FROM bounds.first_at::timestamptz) + "
                   "(extract(epoch FROM bounds.last_at::timestamptz) - "
                   "extract(epoch FROM bounds.first_at::timestamptz)) * bucket.bucket_no / "
                   + str(points))
    upper_epoch = ("extract(epoch FROM bounds.first_at::timestamptz) + "
                   "(extract(epoch FROM bounds.last_at::timestamptz) - "
                   "extract(epoch FROM bounds.first_at::timestamptz)) * (bucket.bucket_no + 1) / "
                   + str(points))
    timestamp_format = "'YYYY-MM-DD\"T\"HH24:MI:SS.US'"
    lower_at = "to_char(to_timestamp(" + lower_epoch + ") AT TIME ZONE 'UTC'," + timestamp_format + ") || '+00:00'"
    upper_at = "to_char(to_timestamp(" + upper_epoch + ") AT TIME ZONE 'UTC'," + timestamp_format + ") || '+00:00'"
    sql = ('WITH sampling_labels AS MATERIALIZED ' + latest_labels +
           ' SELECT picked.*,l.match_id AS _sample_label_match,bucket.bucket_no AS _sample_bucket, '
           'bounds.first_at AS _sample_first_at,selected_point.at AS _sample_at '
           'FROM sampling_labels l CROSS JOIN LATERAL '
           '(SELECT (' + endpoint.format(direction='ASC') + ') AS first_at, '
           '(' + endpoint.format(direction='DESC') + ') AS last_at OFFSET 0) bounds '
           'CROSS JOIN LATERAL (SELECT bucket_no FROM generate_series(0,' +
           str(points - 1) + ') AS fixed_buckets(bucket_no) WHERE bounds.first_at<>bounds.last_at '
           'OR bucket_no=0 OFFSET 0) AS bucket '
           'CROSS JOIN LATERAL (SELECT ' + lower_at + ' AS lower_at, ' + upper_at + ' AS upper_at) time_bucket '
           'LEFT JOIN LATERAL (SELECT i.at FROM model_inputs i WHERE ' + eligible_axis +
           ' AND i.at>=time_bucket.lower_at AND i.at<=time_bucket.upper_at '
           'AND (i.at<time_bucket.upper_at OR '
           '((bucket.bucket_no=' + str(points - 1) + ' OR bounds.first_at=bounds.last_at) '
           'AND i.at<=time_bucket.upper_at)) '
           'ORDER BY i.at LIMIT 1) selected_point ON TRUE '
           'LEFT JOIN LATERAL (SELECT grouped.* FROM (SELECT identities.*, '
           '1 + COALESCE(SUM(_curve_rows) OVER (ORDER BY '
           'hashtextextended(concat_ws(\'|\',algorithm,market),bucket.bucket_no),'
           'algorithm,market ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),0) AS _group_start '
           'FROM (SELECT i.algorithm,i.market,COUNT(*) AS _curve_rows FROM model_inputs i '
           'WHERE i.match_id=l.match_id AND i.at=selected_point.at '
           'GROUP BY i.algorithm,i.market) identities) grouped '
           'WHERE grouped._group_start<=CASE WHEN bounds.first_at=bounds.last_at THEN ' +
           str(max_per_match) + ' ELSE ' + str(rows_per_point) +
           '+CASE WHEN bucket.bucket_no<' + str(extra_rows) + ' THEN 1 ELSE 0 END END) selected_curve ON TRUE '
           'LEFT JOIN LATERAL (SELECT checked.* FROM (SELECT i.*, ('+_curve_complete_sql('model_inputs')+') AS _curve_complete, '
           'BOOL_AND(COALESCE((' + qualified_time + ' AND i.data_version=%s '
           'AND i.model_version=%s AND i.feature_version=%s),FALSE)) OVER () AS _curve_valid '
           'FROM model_inputs i WHERE i.match_id=l.match_id AND i.at=selected_point.at '
           'AND i.algorithm=selected_curve.algorithm AND i.market=selected_curve.market) checked '
           'WHERE checked._curve_valid) picked ON TRUE')
    return sql, (*version_args, *version_args, *version_args, *version_args)


def _selected_contract_rows(source: Iterable[sqlite3.Row], totals: Mapping[str, int],
                            cap: int | None, *,
                            curve_totals: Mapping[str,int]) -> Iterable[tuple[sqlite3.Row, bool]]:
    """Turn the soft row target into an evenly spaced complete-curve quota.

    Counting each row quantile as one retained curve multiplies the target by
    the number of lines/directions. Use metadata-only mean curve size instead;
    actual rows remain variable when curves have unequal sizes.
    """
    if cap is None:
        for row in source:
            yield row, True
        return
    seen: dict[str, int] = {}
    def identity(row: sqlite3.Row) -> tuple:
        return tuple(row[k] for k in ('match_id', 'at', 'algorithm', 'market'))
    for key, members in groupby(source, identity):
        group = list(members)
        mid = str(key[0])
        before = seen.get(mid, 0)
        after = before + 1
        seen[mid] = after
        curve_total = curve_totals[mid]
        curve_cap = min(curve_total,max(1,cap*curve_total//totals[mid]))
        selected = after * curve_cap // curve_total > before * curve_cap // curve_total
        for row in group:
            yield row, selected


def dataset(db: Path, inputs_path: Path | None = None, *, audit: dict[str, Any] | None = None,
            max_per_match: int | None = None,
            cohort_labels: Mapping[str, Any] | None = None,
            input_cutoff: str | None = None) -> list[dict[str, Any]]:
    if max_per_match is not None and (isinstance(max_per_match, bool) or not isinstance(max_per_match, int) or max_per_match < 1):
        raise ValueError('每场训练样本上限必须为正整数')
    projection = projection_target_from_env()
    if projection is not None:
        return _dataset_postgres(projection, audit=audit, max_per_match=max_per_match,
                                 cohort_labels=cohort_labels,input_cutoff=input_cutoff)
    if not db.exists():
        return []
    con = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    rows = []
    counts = {'input_records': 0, 'input_matches': 0, 'valid_training_decisions': 0, 'waiting_for_label': 0,
              'quarantined_inputs': 0, 'quarantined_ledger': 0, 'ungraded_inputs': 0,
              'quarantined_missing_time_contract': 0, 'eligible_time_contract': 0}
    source_count = 0
    # Most rows in a match share the same score/line/basis.  Settlement is
    # deterministic, so memoizing it removes millions of repeated JSON parses
    # and split-line calculations while preserving the exact target function.
    target_cache: dict[tuple[Any, ...], tuple[int, float] | None] = {}
    match_totals: dict[str, int] = {}
    match_curve_totals: dict[str,int] = {}
    try:
        finals = dict(cohort_labels) if cohort_labels is not None else labels(con)
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
                        if input_cutoff is not None:
                            cutoff_at = _label_time(input_cutoff)
                            if cutoff_at is None:
                                raise ValueError('冻结训练输入截点必须是带时区的时间')
                            qualified_time_filter += " AND i.at<='"+cutoff_at.isoformat()+"'"
                        available_filter = (' FROM inputs AS i JOIN training_labels AS l '
                            'ON l.match_id=i.match_id WHERE i.data_version=? AND i.model_version=? '
                            'AND i.feature_version=? AND ' + qualified_time_filter +
                            ' AND l.available_at>i.at')
                        counts['labelled_time_contract'] = inputs.execute(
                            'SELECT count(*)' + available_filter, version_args).fetchone()[0]
                        counts['waiting_for_label'] = (counts['eligible_time_contract'] -
                                                       counts['labelled_time_contract'])
                        # Cross-line coherence needs every outcome/line from
                        # the original market curve. Dropping just an invalid
                        # clock row would silently change the evaluated curve.
                        curve_filter = (" AND NOT EXISTS (SELECT 1 FROM inputs AS j "
                            "WHERE j.match_id=i.match_id AND j.at=i.at AND j.algorithm=i.algorithm "
                            "AND j.market=i.market AND j.data_version=i.data_version "
                            "AND j.model_version=i.model_version AND j.feature_version=i.feature_version "
                            "AND NOT COALESCE((" + qualified_time_filter.replace('i.', 'j.') + "),0))")
                        complete_curve_filter = available_filter + curve_filter
                        complete_count = inputs.execute('SELECT count(*)' + complete_curve_filter,
                                                        version_args).fetchone()[0]
                        counts['quarantined_incomplete_market_curve_inputs'] = (
                            counts['labelled_time_contract'] - complete_count)
                        complete_curve_filter += ' AND ('+_curve_complete_sql('inputs')+')'
                        complete_outcomes = inputs.execute('SELECT count(*)'+complete_curve_filter,version_args).fetchone()[0]
                        counts['quarantined_incomplete_outcome_curve_inputs'] = complete_count-complete_outcomes
                        # Do not sort millions of rows in a temporary SQLite
                        # result. The final sampled rows are sorted in Python.
                        source_query = 'SELECT i.*' + complete_curve_filter
                        if max_per_match is not None:
                            source_query += ' ORDER BY i.match_id,i.at,i.algorithm,i.market,i.line,i.outcome'
                        source = inputs.execute(source_query, version_args)
                        for mid,total,curves in inputs.execute(
                            'SELECT match_id,SUM(curve_rows),COUNT(*) FROM '
                            '(SELECT i.match_id,COUNT(*) AS curve_rows'+complete_curve_filter+
                            ' GROUP BY i.match_id,i.at,i.algorithm,i.market) curve_sizes GROUP BY match_id',version_args):
                            match_totals[str(mid)] = int(total)
                            match_curve_totals[str(mid)] = int(curves)
                    else:
                        counts['quarantined_missing_time_contract'] = versioned_rows
                else:
                    counts['quarantined_inputs'] = inputs.execute('SELECT count(*) FROM inputs').fetchone()[0]
                for r, selected in _selected_contract_rows(source, match_totals, max_per_match,
                                                           curve_totals=match_curve_totals):
                    mid = str(r['match_id'])
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
                    else:
                        source_count += 1
                    if not selected:
                        counts['valid_training_decisions'] += int(paid is not None)
                        continue
                    rows.append({'match_id': r['match_id'], 'at': r['at'], 'settled_at': final['settled_at'],
                             'x': json.loads(r['x']), **_classification_fields(paid),
                             'baseline': r['baseline'], 'algorithm': r['algorithm'],
                             'market': r['market'], 'line': r['line'], 'outcome': r['outcome']})
                    counts['valid_training_decisions'] += int(paid is not None)
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
                       counts['quarantined_missing_time_contract'] +
                       counts.get('quarantined_incomplete_market_curve_inputs', 0))
            invalid += counts.get('quarantined_incomplete_outcome_curve_inputs',0)
            audit.update(counts, invalid_training_records=invalid,
                         valid_context_records=source_count+counts['ungraded_inputs'],
                         classification_unscored_records=counts['ungraded_inputs'],
                         classification_unscored_basis='完整资格上下文中的走盘或结算暂不可评分；不属于无效输入',
                         prediction_context_records=len(rows),
                         prediction_context_matches=len({str(r['match_id']) for r in rows}),
                         sample_valid_training_matches=len({str(r['match_id']) for r in rows if r['graded']}),
                         source_eligible_decisions=source_count,
                         training_max_rows_per_match=max_per_match,
                         counts_scope='full_inputs', counts_estimated=False,
                         full_audit_complete=True, sampled=False,
                         training_sampled=max_per_match is not None,
                         source_eligible_decisions_scope='full_inputs',
                         sampling_cap_basis='soft row target; preserve every line/outcome in selected market curves',
                         sampling_curve_quota_basis='row target divided by metadata-only mean curve size; minimum one complete curve',
                         sampling_row_bound=None,
                         sampling_unit='match_id,at,algorithm,market',
                         sampling_method=('evenly_spaced_market_curves_per_match' if max_per_match is not None
                                          else 'all_qualified_inputs'),
                         projection_backend='sqlite')
        rows.sort(key=lambda row: (row['at'], row['match_id'], row['algorithm'],
                                  row['market'], row['line'], row['outcome']))
        return rows
    finally:
        con.close()


COHORT_CURVE_PROBE_LIMIT = 16
COHORT_MATCH_PROBE_LIMIT = 128
COHORT_CACHE_VERSION = 'complete-clock-market-curves-v1'
COHORT_METADATA_COLUMNS = ('match_id','at','algorithm','market','line','outcome','cutoff_ms',
                           'decision_received_at_ms','quote_received_at_ms','entry_score','settlement_basis')


def _cohort_curve_graded(rows: list[dict[str, Any]], final: Mapping[str, Any]) -> bool:
    """Certify a complete, clock-qualified curve without reading feature JSON."""
    if not rows:
        return False
    for row in rows:
        at, available = _label_time(row.get('at')), _label_time(final.get('settled_at'))
        cutoff = row.get('cutoff_ms')
        if (at is None or available is None or at >= available or isinstance(cutoff,bool)
                or not isinstance(cutoff,(int,float)) or not math.isfinite(cutoff)
                or not 0 < cutoff <= at.timestamp()*1000
                or any(isinstance(row.get(key),bool) or not isinstance(row.get(key),(int,float))
                       or not math.isfinite(row[key]) or not 0 < row[key] <= cutoff
                       for key in ('decision_received_at_ms','quote_received_at_ms'))):
            return False
    return _complete_market_curve(rows) and any(target(row,final) is not None for row in rows)


def _cohort_cache(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
        if value.get('contract') == COHORT_CACHE_VERSION and value.get('versions') == [DATA_VERSION,MODEL_VERSION,FEATURE_VERSION]:
            return value.get('matches',{})
    except (OSError,ValueError,TypeError,AttributeError):
        pass
    return {}


def _persist_cohort_cache(path: Path, matches: dict[str, Any]) -> bool:
    temporary = None
    try:
        path.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile('w',dir=path.parent,prefix='.qualified-cohort-',delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({'contract':COHORT_CACHE_VERSION,'versions':[DATA_VERSION,MODEL_VERSION,FEATURE_VERSION],
                       'matches':matches},handle,sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary,path)
        return True
    except OSError:
        return False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _cohort_metadata_probe(connection: Any, mid: str, final: Mapping[str, Any], *,
                           postgres: bool, cursor_after: list[str] | None,
                           checked_at: str) -> tuple[list[list[dict[str, Any]]], list[str] | None, bool]:
    marker = '%s' if postgres else '?'
    table = 'model_inputs' if postgres else 'inputs'
    where = ('match_id='+marker+' AND data_version='+marker+' AND model_version='+marker+
             ' AND feature_version='+marker+' AND at<'+marker+' AND at<='+marker)
    args: list[Any] = [mid,DATA_VERSION,MODEL_VERSION,FEATURE_VERSION,final['settled_at'],checked_at]
    if cursor_after:
        where += ' AND (at,algorithm,market)<('+','.join(marker for _ in range(3))+')'
        args.extend(cursor_after)
    keys_query = ('SELECT DISTINCT at,algorithm,market FROM '+table+' WHERE '+where+
                  ' ORDER BY at DESC,algorithm DESC,market DESC LIMIT '+marker)
    args.append(COHORT_CURVE_PROBE_LIMIT+1)
    cursor = connection.cursor()
    try:
        cursor.execute(keys_query,tuple(args))
        keys = [tuple(str(value) for value in row) for row in cursor.fetchall()]
        result = []
        for at,algorithm,market in keys[:COHORT_CURVE_PROBE_LIMIT]:
            cursor.execute('SELECT '+','.join(COHORT_METADATA_COLUMNS)+' FROM '+table+
                ' WHERE match_id='+marker+' AND at='+marker+' AND algorithm='+marker+' AND market='+marker+
                ' AND data_version='+marker+' AND model_version='+marker+' AND feature_version='+marker+
                ' ORDER BY line,outcome', (mid,at,algorithm,market,DATA_VERSION,MODEL_VERSION,FEATURE_VERSION))
            result.append([dict(zip(COHORT_METADATA_COLUMNS,row)) for row in cursor.fetchall()])
            if _cohort_curve_graded(result[-1],final):
                return result,None,False
        more = len(keys)>COHORT_CURVE_PROBE_LIMIT
        return result,list(keys[min(len(keys),COHORT_CURVE_PROBE_LIMIT)-1]) if keys else None,more
    finally:
        cursor.close()


def training_cohort_progress(db: Path, inputs_path: Path | None = None, *,
                             consumed_match_ids: Iterable[str] = ()) -> dict[str, Any]:
    """Incrementally certify labelled match identities using metadata only.

    Certificates rely on immutable inputs and are invalidated by label revision
    changes. Negative/partial scans also track the latest version-qualified
    timestamp; newly appended decisions restart their bounded keyset scan.
    Partial certification never advertises a ready training cycle.
    """
    started = time.monotonic()
    checked_at = datetime.now(timezone.utc).isoformat()
    projection = projection_target_from_env()
    source_identity = (projection.backend+'|'+projection.location if projection is not None else
                       'sqlite|'+str(db.resolve())+'|'+str(inputs_path.resolve() if inputs_path else ''))
    digest = hashlib.sha256('|'.join((DATA_VERSION,MODEL_VERSION,FEATURE_VERSION,source_identity)).encode()).hexdigest()[:16]
    cache_path = db.parent/'models'/('qualified-cohort-'+digest+'.json')
    cache = _cohort_cache(cache_path)
    axes: dict[str,str] = {}
    context: ContextManager[Any] | None
    if projection is not None:
        context = readonly_snapshot(projection)
    elif inputs_path is not None and inputs_path.exists() and db.exists():
        context = closing(sqlite3.connect(inputs_path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2))
    else:
        context = None
    finals: dict[str,dict[str,Any]] = {}
    certified: set[str] = set()
    partial: set[str] = set()
    examined = cache_hits = 0
    if context is not None:
        with context as connection:
            if projection is not None:
                finals = _labels_postgres(connection,fetch_size=projection.fetch_size)
            else:
                connection.execute('BEGIN')
                with closing(sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as ledger:
                    finals = labels(ledger)
            checked_time = datetime.fromisoformat(checked_at)
            finals = {str(mid):final for mid,final in finals.items()
                      if (available := _label_time(final.get('settled_at'))) is not None
                      and available <= checked_time}
            ids = sorted(finals)
            cursor = connection.cursor()
            try:
                if projection is not None:
                    for offset in range(0,len(ids),POSTGRES_SAMPLE_MATCH_BATCH):
                        batch = ids[offset:offset+POSTGRES_SAMPLE_MATCH_BATCH]
                        cursor.execute('SELECT l.match_id,i.at FROM unnest(%s::text[],%s::text[]) '
                            'AS l(match_id,available_at) CROSS JOIN LATERAL '
                            '(SELECT at FROM model_inputs WHERE match_id=l.match_id AND data_version=%s '
                            'AND model_version=%s AND feature_version=%s AND at<l.available_at AND at<=%s '
                            'ORDER BY at DESC LIMIT 1) i',
                            (batch,[finals[mid]['settled_at'] for mid in batch],DATA_VERSION,MODEL_VERSION,FEATURE_VERSION,checked_at))
                        axes.update((str(mid),str(at)) for mid,at in cursor.fetchall())
                else:
                    for mid in ids:
                        cursor.execute('SELECT at FROM inputs WHERE match_id=? AND data_version=? '
                            'AND model_version=? AND feature_version=? AND at<? AND at<=? ORDER BY at DESC LIMIT 1',
                            (mid,DATA_VERSION,MODEL_VERSION,FEATURE_VERSION,finals[mid]['settled_at'],checked_at))
                        row = cursor.fetchone()
                        if row:
                            axes[mid] = str(row[0])
            finally:
                cursor.close()
            for mid,axis in axes.items():
                final = finals[mid]
                label_hash = hashlib.sha256(json.dumps(final,sort_keys=True).encode()).hexdigest()
                previous = cache.get(mid,{})
                if previous.get('label_hash') != label_hash or (not previous.get('verified') and previous.get('axis') != axis):
                    previous = {}
                if previous.get('verified'):
                    certified.add(mid)
                    cache_hits += 1
                    continue
                if previous.get('exhausted'):
                    cache_hits += 1
                    continue
                if examined>=COHORT_MATCH_PROBE_LIMIT:
                    partial.add(mid)
                    continue
                examined += 1
                curves,after,more = _cohort_metadata_probe(connection,mid,final,postgres=projection is not None,
                    cursor_after=previous.get('after'),checked_at=checked_at)
                valid = any(_cohort_curve_graded(curve,final) for curve in curves)
                cache[mid] = {'label_hash':label_hash,'axis':axis,'verified':valid,'exhausted':not more,
                              'after':after,'checked_at':checked_at}
                if valid:
                    certified.add(mid)
                elif more:
                    partial.add(mid)
    qualified_labels = {mid:finals[mid] for mid in sorted(certified)}
    saved = _persist_cohort_cache(cache_path,cache)
    consumed = {str(mid) for mid in consumed_match_ids}
    return {'match_ids':sorted(certified),'eligible_matches':len(certified),'new_matches':len(certified-consumed),
            'eligible_decisions':None,'counts_scope':'qualified_labelled_match_cohort',
            'cohort_verified':not partial,'coverage_status':'partial' if partial else 'complete',
            'unverified_candidate_matches':len(partial),'candidate_matches':len(axes),
            'label_matches':len(finals),'examined_matches':examined,'certificate_cache_hits':cache_hits,
            'certificate_cache_persisted':saved,'qualified_labels':qualified_labels,
            'qualification_basis':'complete version-qualified market curves; metadata clock and settlement checks; immutable features validated by writer',
            'label_cutoff':checked_at,'checked_at':checked_at,'query_seconds':time.monotonic()-started}


SHADOW_WINDOW_LIMIT = 20_000
SHADOW_FETCH_LIMIT = 2_000
SHADOW_LABEL_FETCH_LIMIT = 64
SHADOW_SETTLED_MATCHES_PER_CYCLE = 4
SHADOW_SETTLED_MATCH_LIMIT = 64
SHADOW_CONTRACTS_PER_MATCH = 128
SHADOW_AWAITING_MATCH_LIMIT = 64
SHADOW_AWAITING_RETRY_S = 2 * 60 * 60
SHADOW_IDENTITY = ('at', 'match_id', 'algorithm', 'market', 'line', 'outcome')
SHADOW_CURSOR = ('predicted_at', *SHADOW_IDENTITY)


def _prediction_identity(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(row[key]) for key in SHADOW_IDENTITY)


def _bounded_prediction_curves(rows: list[dict[str, Any]], target_rows: int) -> list[dict[str, Any]]:
    """Apply a soft row target without splitting any observed market curve.

    Pending cursor batches may not yet contain every direction. Preserve their
    partial rows until the persisted complete curve can be reconciled, rather
    than treating a partial batch as an invalid prediction.
    """
    unique = {_prediction_identity(row):row for row in rows}
    groups: dict[tuple[str,...],list[dict[str,Any]]] = {}
    for row in sorted(unique.values(),key=lambda r:(str(r.get('predicted_at','')),
                                                   _prediction_identity(r)),reverse=True):
        key = tuple(str(row[name]) for name in ('at','match_id','algorithm','market'))
        groups.setdefault(key,[]).append(row)
    selected: list[dict[str,Any]] = []
    for group in groups.values():
        if len(selected)>=target_rows:
            break
        selected.extend(group)
    return sorted(selected,key=_prediction_identity)


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


def persist_prospective_predictions(db: Path, path: Path, rows: list[dict[str,Any]], *,
                                   target: ProjectionTarget | None = None) -> list[dict[str,Any]]:
    """Validate labels and confirm an outbox batch in one reused PG session."""
    target = target or projection_target_from_env()
    if target is None:
        prospective = prospective_prediction_rows(db,rows)
        write_predictions(path,prospective)
        return prospective
    if not rows:
        return []
    prepare_postgres_schema(target)
    mids = sorted({str(row['match_id']) for row in rows})
    with persistent_writer(target) as connection:
        finals = {}
        for offset in range(0,len(mids),SHADOW_FETCH_LIMIT):
            finals.update(_shadow_label_subset(connection,mids[offset:offset+SHADOW_FETCH_LIMIT]))
        prospective = _without_known_settled_predictions(rows,finals)
        cursor = connection.cursor()
        try:
            _insert_postgres_rows(cursor,'model_predictions',PREDICTION_COLUMNS,
                [tuple(row.get(name) for name in PREDICTION_COLUMNS) for row in prospective],
                '(version,match_id,at,algorithm,market,line,outcome) DO NOTHING')
        finally:
            cursor.close()
    return prospective


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


def _shadow_profit_summary(rows: list[dict[str, Any]], probabilities: list[float], *,
                           first_only: bool) -> dict[str, Any]:
    """Financial population is independent from binary-label availability.

    Earliest complete contract wins before inspecting settlement/odds. A bad
    price or void first observation cannot be replaced by a profitable later
    observation. At equal times the actual ensemble route has priority.
    """
    groups: dict[tuple, list[int]] = {}
    for i, row in enumerate(rows):
        groups.setdefault((str(row['match_id']),row['at'],str(row['algorithm']),
                           row['market'],str(row['line'])), []).append(i)
    chosen = []
    incomplete = 0
    for indices in groups.values():
        market = str(rows[indices[0]]['market'])
        expected = {'home','draw','away'} if market.startswith('HAD') else (
            {'over','under'} if market.startswith('OU') else {'home','away'} if market.startswith('AH') else set())
        if not expected or len(indices) != len(expected) or {rows[i]['outcome'] for i in indices} != expected:
            incomplete += 1
            continue
        chosen.append(rows[max(indices,key=lambda i:(probabilities[i],str(rows[i]['outcome'])))])
    chosen.sort(key=lambda row: (_label_time(row['at']), str(row['match_id']),
                               row['algorithm'] != 'economic_ensemble',str(row['algorithm']),
                               str(row['market']),str(row['line'])))
    statuses = {status:0 for status in (*GRADED,'push')}
    seen: set[tuple] = set()
    matches: set[str] = set()
    excluded = missing_odds = duplicates = 0
    profit = 0.0
    for pick in chosen:
        identity = (str(pick['match_id']),str(pick['market']),str(pick['line']))
        if first_only and identity in seen:
            duplicates += 1
            continue
        seen.add(identity)
        status = pick['settlement_status']
        if status not in statuses:
            excluded += 1
            continue
        odds = pick.get('odds')
        if (isinstance(odds,bool) or not isinstance(odds,(int,float))
                or not math.isfinite(odds) or odds<=1):
            missing_odds += 1
            continue
        statuses[status] += 1
        profit += pnl_for(status,float(odds))
        matches.add(str(pick['match_id']))
    stakes = sum(statuses.values())
    return {'stake_per_contract':1.0,'stake_units':float(stakes),'profit_units':profit,
            'roi':profit/stakes if stakes else None,'selected_contracts':stakes,'matches':len(matches),
            **statuses,'unverifiable_settlement_contracts':excluded,'missing_odds_contracts':missing_odds,
            'incomplete_contracts':incomplete,'duplicate_contracts':duplicates,
            'deduplication': 'first match/market/line; ensemble priority for equal timestamps' if first_only
                             else 'each match/time/algorithm/market/line',
            'basis':'模拟本金1；最高约束概率方向；走盘本金1净0；半赢(赔率−1)/2、半输−0.5；不可核验结算、缺失赔率独立排除；非真实账户收益'}


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
            valid.append({**raw, **_classification_fields(paid),
                          'settlement_status': _settlement_status(raw, final),
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
            if valid[i]['graded']:
                by_match.setdefault(str(valid[i]['match_id']), []).append((valid[i]['y'], valid[i]['observation_weight']))
        score['asian_payment_accuracy'] = (sum(sum(y*w for y,w in pairs)/sum(w for _,w in pairs)
            for pairs in by_match.values())/len(by_match)) if by_match else None
        score['asian_selected_contracts'] = sum(valid[i]['graded'] for i in selected)
        score['asian_payment_basis'] = '每个盘口时点选择最高概率方向；半赢半输按0.5本金、比赛等权；走盘不计有效输赢分母'
        score['unit_stake'] = _shadow_profit_summary(valid, coherent, first_only=True)
        score['unit_stake_by_time'] = _shadow_profit_summary(valid, coherent, first_only=False)
        algorithms = sorted({str(row['algorithm']) for row in valid})
        score['unit_stake_by_algorithm'] = {}
        for algorithm in algorithms:
            indices = [i for i, row in enumerate(valid) if row['algorithm'] == algorithm]
            subgroup, subgroup_probabilities = [valid[i] for i in indices], [coherent[i] for i in indices]
            score['unit_stake_by_algorithm'][algorithm] = {
                'first_contract': _shadow_profit_summary(subgroup, subgroup_probabilities, first_only=True),
                'by_time': _shadow_profit_summary(subgroup, subgroup_probabilities, first_only=False)}
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
                       'excluded_predictions': excluded, 'settled_matches': len({r['match_id'] for r in valid}),
                       'prediction_context_records':len(valid),
                       'graded_predictions':sum(row['graded'] for row in valid),
                       'graded_matches':len({r['match_id'] for r in valid if r['graded']})},
            'model': scores[0], 'baseline': scores[1],
            'labels': {str(mid): {'version': final.get('label_version'),
                                  'first_available_at': final.get('first_label_available_at') or final['settled_at'],
                                  'latest_available_at': final['settled_at']}
                       for mid, final in finals.items() if str(mid) in settled_mids},
            'latency': {f'p{percentile}_ms': latencies[min(len(latencies)-1, int((len(latencies)-1)*percentile/100))]
                        if latencies else None for percentile in (50,95,99)}}


def _shadow_label_events(connection: Any, model: Mapping[str, Any],
                         label_cursor: list[Any] | None) -> tuple[list[dict[str, Any]], bool]:
    """Merge two bounded, indexed chronological terminal-label streams."""
    available = _label_time(model.get('available_at') or model['updated_at'])
    if available is None:
        raise ValueError('模型可用时间无效')
    start = label_cursor
    if start is None:
        first_cursor = connection.cursor()
        try:
            first_cursor.execute('SELECT predicted_at FROM model_predictions '
                'WHERE version=%s AND data_version=%s AND model_version=%s AND feature_version=%s '
                'AND predicted_at IS NOT NULL ORDER BY predicted_at,at,match_id,algorithm,market,line,outcome LIMIT 1',
                (model['version'], DATA_VERSION, MODEL_VERSION, FEATURE_VERSION))
            first_row = first_cursor.fetchone()
        finally:
            first_cursor.close()
        first = _label_time(first_row[0]) if first_row else None
        if first is None:
            return [], False
        # A candidate can predate actual shadow collection by hours. Scores
        # already known before the very first persisted prediction cannot
        # produce prospective evidence and must not flood the retry queue.
        start = [max(available.timestamp(),first.timestamp()), '']
    parts, arguments = [], []
    for table in ('model_labels', 'label_revisions'):
        parts.append('(SELECT match_id,revision_id,shadow_label_available_epoch(available_at) available_epoch '
                     'FROM ' + table + " WHERE status IN ('won','lost','half_won','half_lost','push','void') "
                     'AND shadow_label_available_epoch(available_at) IS NOT NULL '
                     "AND ft_score ~ '" + SHADOW_LABEL_SCORE_PATTERN + "' "
                     'AND (shadow_label_available_epoch(available_at),revision_id) > (%s,%s) '
                     'ORDER BY shadow_label_available_epoch(available_at),revision_id LIMIT %s)')
        arguments.extend([*start, SHADOW_LABEL_FETCH_LIMIT])
    cursor = connection.cursor()
    try:
        # Duplicate copies of the same immutable revision in migration/live
        # projections are one event; bounded UNION sorts at most 128 rows.
        cursor.execute('SELECT * FROM (' + ' UNION '.join(parts) + ') events '
                       'ORDER BY available_epoch,revision_id LIMIT %s',
                       (*arguments, SHADOW_LABEL_FETCH_LIMIT))
        events = [_pg_row(cursor, values) for values in cursor.fetchall()]
        return events, len(events) == SHADOW_LABEL_FETCH_LIMIT
    finally:
        cursor.close()


def _sample_shadow_contracts(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Sample complete market curves, never isolate lines before coherence."""
    unique = {_prediction_identity(row): row for row in rows}
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in sorted(unique.values(), key=lambda r:_prediction_identity(r), reverse=True):
        key = tuple(str(row[k]) for k in ('match_id','at', 'algorithm', 'market'))
        groups.setdefault(key, []).append(row)
    result: list[dict[str, Any]] = []
    accepted = 0
    complete = []
    for group in groups.values():
        if _complete_market_curve(group):
            complete.append(group)
    for group in complete[:SHADOW_CONTRACTS_PER_MATCH]:
        result.extend({key:value for key,value in row.items() if key != 'x'} for row in group)
        accepted += 1
    return result, len(complete) > accepted


def _shadow_match_predictions(connection: Any, model: Mapping[str, Any], mid: str) -> dict[str, Any]:
    cursor = connection.cursor()
    try:
        # First bound curve identities through the immutable PK. A row LIMIT
        # may cut a curve's lines/outcomes and alter cross-line projection.
        columns = ','.join(key for key in PREDICTION_COLUMNS if key != 'x')
        cursor.execute('WITH curve_keys AS MATERIALIZED (SELECT DISTINCT at,algorithm,market '
                       'FROM model_predictions WHERE version=%s AND match_id=%s '
                       'AND data_version=%s AND model_version=%s AND feature_version=%s '
                       'AND predicted_at IS NOT NULL ORDER BY at DESC,algorithm DESC,market DESC LIMIT %s) '
                       'SELECT '+','.join('p.'+name for name in columns.split(','))+' FROM curve_keys k '
                       'JOIN model_predictions p ON p.at=k.at AND p.algorithm=k.algorithm AND p.market=k.market '
                       'WHERE p.version=%s AND p.match_id=%s AND p.data_version=%s AND p.model_version=%s '
                       'AND p.feature_version=%s ORDER BY p.at DESC,p.algorithm DESC,p.market DESC,p.line DESC,p.outcome DESC',
                       (model['version'], mid, DATA_VERSION, MODEL_VERSION, FEATURE_VERSION,
                        SHADOW_CONTRACTS_PER_MATCH+1,model['version'], mid, DATA_VERSION, MODEL_VERSION, FEATURE_VERSION))
        raw = [_pg_row(cursor, values) for values in cursor.fetchall()]
        rows, sampled = _sample_shadow_contracts(raw)
        curves = {tuple(row[k] for k in ('at','algorithm','market')) for row in raw}
        return {'rows':rows, 'sampled':sampled or len(curves) > SHADOW_CONTRACTS_PER_MATCH,
                'fetched_predictions':len(raw), 'refreshed_at':datetime.now(timezone.utc).isoformat()}
    finally:
        cursor.close()


def _prospective_metrics_postgres(projection: ProjectionTarget, model: Mapping[str, Any], *,
                                  state: dict[str, Any] | None = None) -> dict[str, Any]:
    if not compatible_model(model):
        return {}
    state = state if state is not None else {}
    if state.get('version') != model['version']:
        state.clear()
        state.update(version=model['version'], rows=[], cursor=None)
    # Work on new containers: failed queries leave both persistent and in-memory
    # cursors retryable. Upgrade keeps the old prediction cursor/pending rows,
    # while the new label stream starts at candidate availability.
    working = {**state, 'settled_buckets':dict(state.get('settled_buckets', {})),
               'awaiting_prediction_matches':dict(state.get('awaiting_prediction_matches',{})),
               'schema_version':2}
    columns = ','.join(SHADOW_CURSOR)
    with readonly_snapshot(projection) as con:
        cursor = con.cursor()
        try:
            query = ('SELECT * FROM model_predictions WHERE version=%s AND data_version=%s '
                     'AND model_version=%s AND feature_version=%s AND predicted_at IS NOT NULL')
            args: list[Any] = [model['version'], DATA_VERSION, MODEL_VERSION, FEATURE_VERSION]
            if working.get('cursor'):
                query += ' AND (' + columns + ') > (' + ','.join(['%s']*len(SHADOW_CURSOR)) + ')'
                args.extend(working['cursor'])
            query += ' ORDER BY ' + columns + ' LIMIT %s'
            args.append(SHADOW_FETCH_LIMIT)
            cursor.execute(query, tuple(args))
            batch = [_pg_row(cursor, values) for values in cursor.fetchall()]
        finally:
            cursor.close()
        if batch:
            working['cursor'] = [str(batch[-1][key]) for key in SHADOW_CURSOR]
        events, label_has_more = _shadow_label_events(con, model, working.get('label_cursor'))
        consumed: list[dict[str, Any]] = []
        refresh_mids: list[str] = []
        event_lookups = set()
        buckets = working['settled_buckets']
        awaiting = working['awaiting_prediction_matches']
        now = datetime.now(timezone.utc).timestamp()
        expired = [mid for mid, value in awaiting.items() if now-float(value['first_seen_epoch']) >= SHADOW_AWAITING_RETRY_S]
        for mid in expired:
            awaiting.pop(mid)
        working['awaiting_prediction_matches_expired'] = working.get('awaiting_prediction_matches_expired',0)+len(expired)
        for event in events:
            mid = str(event['match_id'])
            if mid not in refresh_mids:
                if len(refresh_mids) == SHADOW_SETTLED_MATCHES_PER_CYCLE:
                    break
                refresh_mids.append(mid)
                event_lookups.add(mid)
                bucket = _shadow_match_predictions(con, model, mid)
                if bucket['rows']:
                    buckets[mid] = bucket
                    awaiting.pop(mid,None)
                else:
                    if mid not in awaiting and len(awaiting) == SHADOW_AWAITING_MATCH_LIMIT:
                        # Missing forecasts from unrelated settled games must
                        # not block the global label stream. Keep the newest
                        # bounded retries and disclose eviction; late writes
                        # can still recover through the prediction cursor.
                        oldest = min(awaiting,key=lambda key:(awaiting[key]['first_seen_epoch'],key))
                        awaiting.pop(oldest)
                        working['awaiting_prediction_matches_evicted'] = working.get('awaiting_prediction_matches_evicted',0)+1
                    awaiting.setdefault(mid,{'first_seen_epoch':now})
                    buckets.pop(mid,None)
            consumed.append(event)
        if consumed:
            last = consumed[-1]
            working['label_cursor'] = [float(last['available_epoch']),str(last['revision_id'])]
        label_has_more = label_has_more or len(consumed) < len(events)
        # A bounded rotation reconciles delayed prediction writes and label
        # corrections without depending solely on the new-label stream.
        rotation = sorted(set(buckets) | set(awaiting))
        offset = int(working.get('refresh_offset', 0))
        visited = 0
        while rotation and visited < len(rotation) and len(refresh_mids) < SHADOW_SETTLED_MATCHES_PER_CYCLE:
            mid = rotation[(offset+visited) % len(rotation)]
            visited += 1
            if mid not in refresh_mids:
                refresh_mids.append(mid)
        working['refresh_offset'] = offset+visited
        for mid in refresh_mids:
            if mid in event_lookups:
                continue  # New events already used their one indexed seek.
            bucket = _shadow_match_predictions(con, model, mid)
            if bucket['rows']:
                buckets[mid] = bucket
                awaiting.pop(mid,None)
            elif mid in buckets:
                # No compatible persisted predictions now: remove derived
                # rows rather than preserving stale data after a repair.
                buckets.pop(mid)
                if len(awaiting) < SHADOW_AWAITING_MATCH_LIMIT:
                    awaiting.setdefault(mid,{'first_seen_epoch':now})
        combined = (list(working.get('rows', [])) +
                    [{key:value for key,value in row.items() if key != 'x'} for row in batch])
        finals = _shadow_label_subset(con, sorted({str(r['match_id']) for r in combined} | set(buckets)))
        prospective = _without_known_settled_predictions(combined, finals)
        working['excluded_replay_predictions'] = (working.get('excluded_replay_predictions', 0) +
                                               len(combined)-len(prospective))
        pending: list[dict[str, Any]] = []
        newly_settled: dict[str, list[dict[str, Any]]] = {}
        for row in prospective:
            mid = str(row['match_id'])
            if mid in finals:
                newly_settled.setdefault(mid, []).append(row)
            else:
                pending.append(row)
        for mid, rows in newly_settled.items():
            previous = buckets.get(mid, {})
            selected, sampled = _sample_shadow_contracts(list(previous.get('rows', []))+rows)
            # A split incremental batch may expose only one direction first.
            # Keep a bounded empty bucket so rotation can fetch the now-atomic
            # complete persisted contract even after its label cursor moved on.
            buckets[mid] = {**previous,'rows':selected,'sampled':previous.get('sampled',False) or sampled}
            if selected:
                awaiting.pop(mid,None)
        # Results arriving an hour later recover their own persisted match
        # predictions through the label cursor even after pending eviction.
        # Recency is first actual settlement availability, not later corrections.
        retained = sorted((mid for mid in buckets if mid in finals),
                          key=lambda mid:(_label_time(finals[mid]['first_label_available_at']),mid),
                          reverse=True)[:SHADOW_SETTLED_MATCH_LIMIT]
        working['settled_buckets'] = {mid:buckets[mid] for mid in retained}
        working['rows'] = _bounded_prediction_curves(pending,SHADOW_WINDOW_LIMIT)
    settled_rows = [row for bucket in working['settled_buckets'].values() for row in bucket['rows']]
    all_rows = settled_rows+working['rows']
    result = _shadow_metrics(all_rows, finals, model)
    result['cursor'] = {'batch_records':len(batch),
                        'has_more':len(batch) == SHADOW_FETCH_LIMIT or label_has_more,
                        'label_records':len(consumed), 'label_has_more':label_has_more,
                        'settled_matches_refreshed':len(refresh_mids)}
    result['counts']['excluded_replay_predictions'] = working['excluded_replay_predictions']
    result['counts']['excluded_replay_scope'] = 'cursor_history_not_window'
    result['counts']['awaiting_prediction_matches'] = len(working['awaiting_prediction_matches'])
    result['counts']['awaiting_prediction_matches_expired'] = working['awaiting_prediction_matches_expired']
    result['counts']['awaiting_prediction_matches_evicted'] = working.get('awaiting_prediction_matches_evicted',0)
    result['windows'] = {'pending':{
        'scope':'recent_pending_predictions', 'from':min((r['at'] for r in working['rows']),default=None),
        'to':max((r['at'] for r in working['rows']),default=None), 'predictions':len(working['rows']),
        'max_predictions':None,'target_predictions':SHADOW_WINDOW_LIMIT,
        'sampling_unit':'complete observed market curve'}, 'settled':{
        'scope':'recent_settled_matches', 'from':min((r['at'] for r in settled_rows),default=None),
        'to':max((r['at'] for r in settled_rows),default=None),
        'matches':len(working['settled_buckets']), 'predictions':len(settled_rows),
        'max_matches':SHADOW_SETTLED_MATCH_LIMIT,'max_contracts_per_match':SHADOW_CONTRACTS_PER_MATCH}}
    result['window'].update(scope='separate_pending_and_settled_windows',max_predictions=None,
                           pending_target_predictions=SHADOW_WINDOW_LIMIT)
    result['sampling'] = {'scope':'settled_window','method':'latest_complete_market_curves_per_match',
        'max_matches':SHADOW_SETTLED_MATCH_LIMIT,'max_contracts_per_match':SHADOW_CONTRACTS_PER_MATCH,
        'per_match_fetch_curve_limit':SHADOW_CONTRACTS_PER_MATCH+1,
        'per_match_fetch_row_limit':None,
        'awaiting_match_limit':SHADOW_AWAITING_MATCH_LIMIT,'awaiting_retry_seconds':SHADOW_AWAITING_RETRY_S,
        'label_start_basis':'max_candidate_available_and_first_actual_prediction',
        'sampled_matches':sum(bool(bucket.get('sampled')) for bucket in working['settled_buckets'].values()),
        'full_population':False,
        'note':'原始预测在PG永久保留；每场先选最近最多129条市场曲线身份，再保留最多128条完整盘口/时点/算法曲线的全部线值和结果；已结窗口仅最近64场，不代表全量正确率'}
    state.clear()
    state.update(working)
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
                selected_columns = [name for name in PREDICTION_COLUMNS if name != 'x' and name in columns]
                rows = [dict(raw) for raw in con.execute(
                    'WITH curve_keys AS (SELECT DISTINCT at,match_id,algorithm,market FROM predictions '
                    'WHERE version=? AND data_version=? AND model_version=? AND feature_version=? '
                    'ORDER BY at DESC,match_id DESC,algorithm DESC,market DESC LIMIT ?) '
                    'SELECT '+','.join('p.'+name for name in selected_columns)+' FROM curve_keys k '
                    'JOIN predictions p ON p.at=k.at AND p.match_id=k.match_id '
                    'AND p.algorithm=k.algorithm AND p.market=k.market '
                    'WHERE p.version=? AND p.data_version=? AND p.model_version=? AND p.feature_version=?',
                    (model['version'], DATA_VERSION, MODEL_VERSION, FEATURE_VERSION, SHADOW_WINDOW_LIMIT,
                     model['version'],DATA_VERSION,MODEL_VERSION,FEATURE_VERSION))]
                rows = _bounded_prediction_curves(rows,SHADOW_WINDOW_LIMIT)
    return _shadow_metrics(rows, finals, model)
