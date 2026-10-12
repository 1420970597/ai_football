"""Indexed SQLite projection of the append-only decision audit journal.

Imports are incremental and transactional. Readers never parse the audit log or
deserialize unbounded entry payloads to compute statistics.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Sequence
from core.live_model import MODEL_VERSION
from core.settlement import _score_of
from store.postgres import PGConnection, connect as pg_connect, json_extract_function_sql, postgres_enabled


FIELDS = {
    'match_id': 'TEXT', 'at': 'TEXT', 'algorithm': 'TEXT', 'competition_type': 'TEXT',
    'trigger': 'TEXT', 'decision': 'TEXT', 'market': 'TEXT', 'league': 'TEXT',
    'line': 'TEXT', 'outcome': 'TEXT',
    'config_version': 'INTEGER', 'status': 'TEXT', 'is_pick': 'INTEGER',
    'decision_count': 'INTEGER', 'decision_event_id': 'TEXT',
    'is_live': 'INTEGER', 'odds': 'REAL', 'pnl': 'REAL', 'confidence': 'REAL',
    'kelly': 'REAL', 'p_fused': 'REAL', 'p_market': 'REAL', 'closing_odds': 'REAL',
    'settled_at': 'TEXT', 'decision_id': 'TEXT', 'experiment_id': 'TEXT',
    'label_available_at': 'TEXT',
    'review_status': 'TEXT', 'research_only': 'INTEGER',
    'model_version': 'TEXT',
}
GRADED = "status IN ('won','lost','half_won','half_lost','push')"
CLASSIFIED = "status IN ('won','lost','half_won','half_lost')"
STATISTICS_MIGRATION = 'decision-statistics-v1'
STATISTICS_INDEXES = {'history': 'competition_type,trigger,algorithm,at',
                      'date': 'at', 'generation': 'model_version,competition_type,trigger,at',
                      'experiment': 'experiment_id,algorithm',
                      'page': "competition_type,trigger,is_pick,(status='pending'),at DESC,identity",
                      'page_type': "competition_type,is_pick,(status='pending'),at DESC,identity",
                      'page_all': "is_pick,(status='pending'),at DESC,identity"}


def prepare_postgres_statistics() -> dict[str, Any]:
    """Build a narrow, transactionally maintained projection off the API path.

    The large append-only payload remains in ``decisions`` for exact detail
    reads.  PostgreSQL statistics read only the small scalar projection.  A
    trigger installed before the backfill preserves concurrent live writes.
    This explicit maintenance operation is never run by a browser request.
    """
    connection = pg_connect()
    names = ['identity', *FIELDS]
    columns = ','.join(k + ' ' + {'TEXT': 'TEXT', 'INTEGER': 'INTEGER',
                                 'REAL': 'DOUBLE PRECISION'}[v] for k, v in FIELDS.items())
    try:
        # Serialize maintenance with a session lock. Publish the trigger
        # before the long backfill so normal decision writes remain live.
        connection.execute('BEGIN').close()
        connection.execute("SELECT pg_advisory_lock(hashtext(?))", (STATISTICS_MIGRATION,)).fetchone()
        connection.execute('CREATE TABLE IF NOT EXISTS ledger_storage_migrations '
                           '(version TEXT PRIMARY KEY, completed_at TIMESTAMPTZ NOT NULL DEFAULT now())').close()
        connection.execute('CREATE TABLE IF NOT EXISTS ledger_history_cache '
                           '(cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, '
                           'refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now())').close()
        ready = connection.execute('SELECT 1 FROM ledger_storage_migrations WHERE version=?',
                                   (STATISTICS_MIGRATION,)).fetchone()
        connection.execute('CREATE TABLE IF NOT EXISTS decision_statistics '
                           '(identity TEXT PRIMARY KEY,' + columns + ')').close()
        projection_names = ','.join(names)
        assignments = ','.join(name + '=EXCLUDED.' + name for name in FIELDS)
        function = ('CREATE OR REPLACE FUNCTION maintain_decision_statistics() RETURNS trigger '
                    'LANGUAGE plpgsql AS $$ BEGIN '
                    "IF TG_OP='DELETE' THEN DELETE FROM decision_statistics WHERE identity=OLD.identity; RETURN OLD; END IF; "
                    "IF TG_OP='UPDATE' AND NEW.identity<>OLD.identity THEN DELETE FROM decision_statistics WHERE identity=OLD.identity; END IF; "
                    'INSERT INTO decision_statistics (' + projection_names + ') VALUES (' +
                    ','.join('NEW.' + name for name in names) + ') ON CONFLICT(identity) DO UPDATE SET ' +
                    assignments + '; RETURN NEW; END $$')
        connection.execute(function).close()
        connection.execute('DROP TRIGGER IF EXISTS decisions_statistics_projection ON decisions').close()
        connection.execute('CREATE TRIGGER decisions_statistics_projection AFTER INSERT OR UPDATE OR DELETE '
                           'ON decisions FOR EACH ROW EXECUTE FUNCTION maintain_decision_statistics()').close()
        connection.commit()
        if ready:
            for name, cols in STATISTICS_INDEXES.items():
                connection.execute('CREATE INDEX IF NOT EXISTS idx_statistics_' + name +
                                   ' ON decision_statistics(' + cols + ')').close()
            connection.commit()
            return {'ready': True, 'backfilled': 0, 'already_ready': True}
        connection.execute('BEGIN').close()
        inserted = connection.execute('INSERT INTO decision_statistics (' + projection_names + ') SELECT ' +
                                      projection_names + ' FROM decisions ON CONFLICT(identity) DO NOTHING')
        backfilled = inserted.rowcount
        inserted.close()
        # A source row can be deleted after the backfill SELECT snapshot but
        # before it reaches this projection. Reconcile that one initial race;
        # subsequent deletes are handled by the already installed trigger.
        connection.execute('DELETE FROM decision_statistics p WHERE NOT EXISTS '
                           '(SELECT 1 FROM decisions d WHERE d.identity=p.identity)').close()
        for name, cols in STATISTICS_INDEXES.items():
            connection.execute('CREATE INDEX IF NOT EXISTS idx_statistics_' + name +
                               ' ON decision_statistics(' + cols + ')').close()
        connection.execute('ANALYZE decision_statistics').close()
        connection.execute('INSERT INTO ledger_storage_migrations(version) VALUES (?)',
                           (STATISTICS_MIGRATION,)).close()
        connection.commit()
        return {'ready': True, 'backfilled': backfilled, 'already_ready': False}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


class LedgerDB:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.backend = "postgres" if postgres_enabled() else "sqlite"
        self.connection: PGConnection | sqlite3.Connection
        if self.backend == "postgres":
            self.connection = pg_connect()
            self._init_postgres()
            return
        self.connection = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA synchronous=FULL')
        self.connection.execute('PRAGMA busy_timeout=10000')
        columns = ','.join(k + ' ' + v for k, v in FIELDS.items())
        self.connection.execute('CREATE TABLE IF NOT EXISTS decisions '
                                '(identity TEXT PRIMARY KEY, revision REAL, payload TEXT,' + columns + ')')
        existing = {row[1] for row in self.connection.execute('PRAGMA table_info(decisions)')}
        missing = [name for name in ('line', 'outcome', 'model_version',
                                     'decision_count', 'decision_event_id',
                                     'label_available_at') if name not in existing]
        if missing:
            # Atomic migration: a crash must not leave added columns unfilled.
            with self.connection:
                self.connection.execute('BEGIN IMMEDIATE')
                for name in missing:
                    self.connection.execute('ALTER TABLE decisions ADD COLUMN ' + name + ' ' + FIELDS[name])
                    if name == 'decision_count':
                        self.connection.execute(
                            "UPDATE decisions SET decision_count=coalesce(CAST(json_extract(payload,'$.decision_count') AS INTEGER),1)")
                    else:
                        self.connection.execute('UPDATE decisions SET ' + name +
                            "=coalesce(CAST(json_extract(payload,'$." + name + "') AS TEXT),'')")
        for name, cols in {
            'history': 'competition_type,trigger,algorithm,at',
            'pending': 'status,match_id', 'date': 'at',
            'performance': 'competition_type,trigger,status,algorithm,settled_at',
            'experiment': 'experiment_id,algorithm',
            'recommendation_match': 'trigger,algorithm,match_id,at',
            'decision_identity': 'decision_id',
            'recommendation_quote': 'trigger,is_pick,algorithm,market,line,outcome,status',
            'generation': 'model_version,competition_type,trigger,at',
        }.items():
            self.connection.execute('CREATE INDEX IF NOT EXISTS idx_' + name + ' ON decisions(' + cols + ')')
        self.connection.execute('CREATE TABLE IF NOT EXISTS imports '
                                '(path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER)')
        # Append-only label snapshots preserve the point-in-time truth available
        # when each result or correction first reached the system.
        self.connection.execute('CREATE TABLE IF NOT EXISTS label_revisions '
                                '(revision_id TEXT PRIMARY KEY, match_id TEXT NOT NULL, available_at TEXT NOT NULL, '
                                'ft_score TEXT NOT NULL, ht_score TEXT, status TEXT NOT NULL, source_revision TEXT NOT NULL, '
                                'revision_order REAL NOT NULL DEFAULT 0)')
        self.connection.execute('CREATE INDEX IF NOT EXISTS idx_label_revisions_match_time '
                                'ON label_revisions(match_id,available_at)')
        self.connection.commit()

    def _init_postgres(self) -> None:
        """Create the remote projection without creating a local SQLite file."""
        columns = ','.join(k + ' ' + {
            'TEXT': 'TEXT', 'INTEGER': 'INTEGER', 'REAL': 'DOUBLE PRECISION'
        }[v] for k, v in FIELDS.items())
        self.connection.execute('CREATE TABLE IF NOT EXISTS decisions '
                                '(identity TEXT PRIMARY KEY, revision DOUBLE PRECISION, payload TEXT,' + columns + ')')
        self.connection.execute('CREATE TABLE IF NOT EXISTS imports '
                                '(path TEXT PRIMARY KEY, size BIGINT, mtime BIGINT)')
        self.connection.execute('CREATE TABLE IF NOT EXISTS label_revisions '
                                '(revision_id TEXT PRIMARY KEY, match_id TEXT NOT NULL, available_at TEXT NOT NULL, '
                                'ft_score TEXT NOT NULL, ht_score TEXT, status TEXT NOT NULL, source_revision TEXT NOT NULL, '
                                'revision_order DOUBLE PRECISION NOT NULL DEFAULT 0)')
        self.connection.execute('CREATE TABLE IF NOT EXISTS model_labels '
                                '(match_id TEXT NOT NULL, available_at TEXT NOT NULL, ft_score TEXT NOT NULL, '
                                'ht_score TEXT, status TEXT NOT NULL, revision_id TEXT PRIMARY KEY, '
                                'revision_order DOUBLE PRECISION NOT NULL DEFAULT 0)')
        self.connection.execute('CREATE TABLE IF NOT EXISTS ledger_storage_migrations '
                                '(version TEXT PRIMARY KEY, completed_at TIMESTAMPTZ NOT NULL DEFAULT now())')
        self.connection.execute('CREATE TABLE IF NOT EXISTS ledger_history_cache '
                                '(cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, '
                                'refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now())')
        for name, cols in {
            'history': 'competition_type,trigger,algorithm,at',
            'pending': 'status,match_id', 'date': 'at',
            'performance': 'competition_type,trigger,status,algorithm,settled_at',
            'experiment': 'experiment_id,algorithm',
            'recommendation_match': 'trigger,algorithm,match_id,at',
            'decision_identity': 'decision_id',
            'recommendation_quote': 'trigger,is_pick,algorithm,market,line,outcome,status',
            'generation': 'model_version,competition_type,trigger,at',
        }.items():
            self.connection.execute('CREATE INDEX IF NOT EXISTS idx_' + name + ' ON decisions(' + cols + ')')
        self.connection.execute('CREATE INDEX IF NOT EXISTS idx_label_revisions_match_time '
                                'ON label_revisions(match_id,available_at)')
        self.connection.execute('CREATE INDEX IF NOT EXISTS idx_model_labels_match_time '
                                'ON model_labels(match_id,available_at)')
        self.connection.execute(json_extract_function_sql())
        self.connection.commit()

    def _statistics_table(self) -> str:
        if self.backend != 'postgres':
            return 'decisions'
        if getattr(self, '_statistics_ready', False):
            return 'decision_statistics'
        ready = self.connection.execute('SELECT to_regclass(?)', ('ledger_storage_migrations',)).fetchone()
        if ready and ready[0]:
            ready = self.connection.execute('SELECT 1 FROM ledger_storage_migrations WHERE version=?',
                                            (STATISTICS_MIGRATION,)).fetchone()
            if ready:
                self._statistics_ready = True
                return 'decision_statistics'
        return 'decisions'

    def __del__(self) -> None:
        try:
            self.connection.close()
        except Exception:
            pass

    @contextmanager
    def reader(self) -> Iterator[LedgerDB]:
        """A separate WAL snapshot lets aggregation run alongside the writer."""
        reader = object.__new__(LedgerDB)
        reader.path = self.path
        reader.backend = self.backend
        reader._statistics_ready = getattr(self, '_statistics_ready', False)
        if self.backend == "postgres":
            reader.connection = pg_connect()
            reader.connection.execute('BEGIN')
            try:
                yield reader
            finally:
                reader.connection.close()
            return
        reader.connection = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
        reader.connection.row_factory = sqlite3.Row
        try:
            reader.connection.execute('BEGIN')
            yield reader
        finally:
            reader.connection.close()

    @staticmethod
    def identity(row: Mapping[str, Any]) -> str:
        key = [row.get(k, '') for k in ('decision_id', 'match_id', 'market', 'line', 'outcome')]
        return hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()

    def put(self, rows: Sequence[Mapping[str, Any]], epoch: Any) -> None:
        columns = ['identity', 'revision', 'payload', *FIELDS]
        sql = ('INSERT INTO decisions (' + ','.join(columns) + ') VALUES (' +
               ','.join('?' for _ in columns) + ') ON CONFLICT(identity) DO UPDATE SET ' +
               ','.join(k + '=excluded.' + k for k in columns[1:]) +
               ' WHERE excluded.revision>=decisions.revision')
        values = []
        for row in rows:
            revision = epoch(row.get('updated_at') or row.get('settled_at') or row['at'])
            defaults = {k: ('' if typ == 'TEXT' else 0) for k, typ in FIELDS.items()}
            values.append([self.identity(row), revision, json.dumps(dict(row), ensure_ascii=False),
                           *[row.get(k, defaults[k]) for k in FIELDS]])
        self.connection.executemany(sql, values)
        labels = []
        for row in rows:
            ft = row.get('ft_score')
            available = str(row.get('label_available_at') or '')
            if (row.get('competition_type') != 'real' or row.get('status') not in
                    ('won', 'lost', 'half_won', 'half_lost', 'push', 'void') or
                    not available or not isinstance(ft, (list, tuple)) or len(ft) != 2):
                continue
            ht = row.get('ht_score')
            identity = [str(row.get('match_id') or ''), available, list(ft), list(ht) if isinstance(ht, (list, tuple)) else None,
                        str(row.get('status') or ''), str(row.get('updated_at') or '')]
            revision_id = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
            labels.append((revision_id, identity[0], available,
                           json.dumps(list(ft)), json.dumps(identity[3]) if identity[3] is not None else None,
                           identity[4], identity[5], float(epoch(available))))
        if labels:
            self.connection.executemany(
                'INSERT INTO label_revisions '
                '(revision_id,match_id,available_at,ft_score,ht_score,status,source_revision,revision_order) '
                'VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING', labels)

    def backfill_observed_labels(self, available_at: str,
                                 source: str = 'late_observed_migration') -> dict[str, int]:
        """Create point-in-time labels for old terminal rows missing a revision.

        Older ledger rows were settled with a score but before the explicit
        ``label_available_at`` field existed.  Their score is durable evidence,
        but their first observation time is not recoverable.  This migration
        records the time it is run as the availability time instead of
        backdating the label to an event or settlement timestamp.  Existing
        revisions are left untouched and the operation is idempotent per match.
        """
        available = str(available_at or '').strip()
        if not available:
            raise ValueError('标签可用时间不能为空')
        terminal = ('won', 'lost', 'half_won', 'half_lost', 'push', 'void')
        existing = {str(row[0]) for row in self.connection.execute(
            'SELECT DISTINCT match_id FROM label_revisions')}
        latest: dict[str, tuple[str, float, dict[str, Any], tuple[int, int], tuple[int, int] | None]] = {}
        scanned = 0
        skipped = 0
        rows = self.connection.execute(
            "SELECT identity,revision,payload FROM decisions "
            "WHERE competition_type='real' AND status IN (?,?,?,?,?,?) "
            "ORDER BY revision DESC,identity", terminal)
        for identity, revision, raw in rows:
            scanned += 1
            try:
                payload = json.loads(raw)
                mid = str(payload.get('match_id') or '')
                ft = _score_of(payload.get('ft_score'))
                ht = _score_of(payload.get('ht_score'))
                status = str(payload.get('status') or '')
            except (TypeError, ValueError, json.JSONDecodeError):
                skipped += 1
                continue
            if not mid or mid in existing or mid in latest or ft is None or status not in terminal:
                skipped += 1
                continue
            latest[mid] = (str(identity), float(revision or 0), payload, ft, ht)
        labels = []
        for mid, (identity, revision, payload, ft, ht) in latest.items():
            source_revision = '%s:%s:%s' % (source, identity, revision)
            revision_key = [mid, available, list(ft), list(ht) if ht is not None else None,
                            str(payload.get('status') or ''), source_revision]
            revision_id = hashlib.sha256(json.dumps(revision_key, ensure_ascii=False).encode()).hexdigest()
            labels.append((revision_id, mid, available, json.dumps(list(ft)),
                           json.dumps(list(ht)) if ht is not None else None,
                           str(payload.get('status') or ''), source_revision, float(revision or 0)))
        if labels:
            with self.connection:
                self.connection.executemany(
                    'INSERT OR IGNORE INTO label_revisions '
                    '(revision_id,match_id,available_at,ft_score,ht_score,status,source_revision,revision_order) '
                    'VALUES (?,?,?,?,?,?,?,?)', labels)
                if self.backend == 'postgres':
                    self.connection.executemany(
                        'INSERT INTO model_labels '
                        '(match_id,available_at,ft_score,ht_score,status,revision_id,revision_order) '
                        'VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                        [(row[1], row[2], row[3], row[4], row[5], row[0], row[7]) for row in labels])
        return {'scanned': scanned, 'backfilled_matches': len(labels), 'skipped': skipped}

    def entries(self, where: str = '1=1', params: Sequence[Any] = (),
                limit: int | None = None, offset: int = 0) -> list:
        table = self._statistics_table()
        sql = 'SELECT payload FROM decisions WHERE ' + where
        sql += ' ORDER BY (status=\'pending\'),at DESC,identity'
        if limit is not None:
            sql += ' LIMIT ? OFFSET ?'
            params = (*params, limit, offset)
            if table == 'decision_statistics':
                # Sort narrow identities, then fetch only the current page's
                # payloads. Sorting the full multi-GB payload heap defeats
                # pagination even with a small LIMIT.
                sql = ('SELECT d.payload FROM decisions d JOIN (SELECT identity,status,at FROM ' + table +
                       ' WHERE ' + where + " ORDER BY (status='pending'),at DESC,identity LIMIT ? OFFSET ?) page "
                       "ON d.identity=page.identity ORDER BY (page.status='pending'),page.at DESC,page.identity")
        return [json.loads(r[0]) for r in self.connection.execute(sql, params)]

    @staticmethod
    def _metric_expressions() -> dict[str, str]:
        staked = '(' + GRADED + ' AND is_pick=1)'
        nonpush = '(' + CLASSIFIED + ' AND is_pick=1)'
        # SQLite coerces predicates to 0/1, while PostgreSQL keeps them as
        # boolean values.  Never pass a predicate directly to SUM: all
        # weighted stake expressions must be explicitly numeric on both
        # backends.
        staked_units = 'CASE WHEN ' + staked + ' THEN 1.0 ELSE 0.0 END'
        win = "CASE status WHEN 'won' THEN 1.0 WHEN 'half_won' THEN 0.5 ELSE 0 END"
        loss = "CASE status WHEN 'lost' THEN 1.0 WHEN 'half_lost' THEN 0.5 ELSE 0 END"
        # SQLite treats predicates as integers; PostgreSQL does not allow
        # SUM(boolean).  Keep every aggregate explicitly numeric so the same
        # query remains valid on both backends.
        def count_if(condition: str) -> str:
            return 'SUM(CASE WHEN ' + condition + ' THEN 1 ELSE 0 END)'

        return {
            'total': 'count(*)', 'matches': 'count(DISTINCT match_id)',
            'legacy_identity_rows': count_if("decision_id=''"),
            **{s: count_if("status='" + s + "'") for s in
               ('pending', 'void')},
            'graded': count_if(GRADED),
            **{s: count_if("is_pick=1 AND status='"+s+"'") for s in ('half_won','half_lost','push')},
            'won': count_if("is_pick=1 AND status IN ('won','half_won')"),
            'lost': count_if("is_pick=1 AND status IN ('lost','half_lost')"),
            'win_stake_units': 'sum(CASE WHEN is_pick=1 THEN ' + win + ' ELSE 0 END)',
            'loss_stake_units': 'sum(CASE WHEN is_pick=1 THEN ' + loss + ' ELSE 0 END)',
            'stake_units': 'sum(' + staked_units + ')',
            'profit_units': 'sum(CASE WHEN ' + staked + ' THEN pnl ELSE 0 END)',
            'avg_odds': 'avg(CASE WHEN ' + staked + ' THEN odds END)',
            'confidence_stake': 'sum(CASE WHEN ' + staked + ' THEN confidence ELSE 0 END)',
            'confidence_profit': 'sum(CASE WHEN ' + staked + ' THEN pnl*confidence ELSE 0 END)',
            'allocated_stake': 'sum(CASE WHEN ' + staked + ' THEN kelly ELSE 0 END)',
            'allocated_profit': 'sum(CASE WHEN ' + staked + ' THEN pnl*kelly ELSE 0 END)',
            'direction_samples': count_if(nonpush + " AND market IN ('HAD','HAD_1H')"),
            'direction_correct': count_if("is_pick=1 AND status='won' AND market IN ('HAD','HAD_1H')"),
            'clv_n': count_if(staked + ' AND is_live=0 AND closing_odds>1 AND odds>1'),
            'clv_mean': 'avg(CASE WHEN ' + staked + ' AND is_live=0 AND closing_odds>1 AND odds>1 THEN odds/closing_odds-1 END)',
            'clv_positive_rate': 'avg(CASE WHEN ' + staked + ' AND is_live=0 AND closing_odds>1 AND odds>1 '
                                  'THEN CASE WHEN odds>closing_odds THEN 1.0 ELSE 0.0 END END)',
            'unpicked_n': count_if(GRADED + ' AND is_pick=0'),
            'unpicked_wins': count_if("is_pick=0 AND status IN ('won','half_won')"),
            'unpicked_losses': count_if("is_pick=0 AND status IN ('lost','half_lost')"),
        }

    @staticmethod
    def _finish_aggregate(r: Dict[str, Any], metrics: Mapping[str, str]) -> Dict[str, Any]:
        for key in metrics:
            # SUM over numeric CASE literals returns Decimal on PG.  The
            # public statistics contract uses JSON numbers, and timeline
            # callers accumulate these values into native floats.
            if isinstance(r[key], Decimal):
                r[key] = float(r[key])
            if key not in ('avg_odds', 'clv_mean', 'clv_positive_rate'):
                r[key] = r[key] or 0
        def ratio(a: str, b: str) -> Any:
            return round(r[a] / r[b], 6) if r[b] else None
        units = r['win_stake_units'] + r['loss_stake_units']
        r['hit_rate'] = round(r['win_stake_units'] / units, 4) if units else None
        r['accuracy'] = r['hit_rate']
        r['accuracy_samples'] = r['won'] + r['lost']
        r['accuracy_units'] = units
        r['roi'] = ratio('profit_units', 'stake_units')
        r['confidence_roi'] = ratio('confidence_profit', 'confidence_stake')
        r['allocated_roi'] = ratio('allocated_profit', 'allocated_stake')
        r['direction_accuracy'] = ratio('direction_correct', 'direction_samples')
        r['profitable_pick_rate'] = round(r['won'] / r['accuracy_samples'], 4) if r['accuracy_samples'] else None
        r['full_won'] = r['won'] - r['half_won']
        r['full_lost'] = r['lost'] - r['half_lost']
        w = r.pop('unpicked_wins')
        n = w + r.pop('unpicked_losses')
        r['unpicked_hit_rate'] = round(w/n, 4) if n else None
        r['metric_basis'] = '模拟每条独立建议1单位本金；正确率按半注权重，走水剔除'
        r['clv_basis'] = '仅赛前同盘口同结果；滚球价格漂移不作为CLV'
        return r

    @staticmethod
    def _group_expression(group: str) -> str:
        return ("substr(at,1,10)" if group in ('date_key', 'settled_date') else
                "CASE WHEN confidence>=0.8 THEN '80–100%' WHEN confidence>=0.6 THEN '60–80%' "
                "WHEN confidence>0 THEN '0–60%' ELSE '未记录' END" if group == 'confidence_band' else
                "CASE WHEN " + GRADED + " THEN 'graded' ELSE 'ungraded' END" if group == 'settlement_bucket' else group)

    def aggregate(self, where: str = '1=1', params: Sequence[Any] = (),
                  group: str | None = None) -> Dict[str, Any]:
        if group and group not in {*FIELDS, 'date_key', 'confidence_band'}:
            raise ValueError('invalid aggregation')
        expression = self._group_expression(group) if group else None
        metrics = self._metric_expressions()
        sql = 'SELECT ' + ((str(expression) + ' AS bucket,') if group else '')
        sql += ','.join(v + ' AS ' + k for k, v in metrics.items()) + ' FROM ' + self._statistics_table() + ' WHERE ' + where
        if group:
            sql += ' GROUP BY ' + str(expression)
        rows = [dict(r) for r in self.connection.execute(sql, params)]
        result = [self._finish_aggregate(r, metrics) for r in rows]
        return {str(r.pop('bucket')): r for r in result} if group else result[0]

    def aggregate_many(self, where: str, params: Sequence[Any],
                       groups: Sequence[str]) -> dict[str, Any]:
        """Compute history totals and groups in one PostgreSQL projection scan."""
        if self.backend != 'postgres':
            return {'overall': self.aggregate(where, params),
                    **{group: self.aggregate(where, params, group) for group in groups}}
        if any(group not in {*FIELDS, 'date_key', 'confidence_band'} for group in groups):
            raise ValueError('invalid aggregation')
        all_groups = [*groups, 'settlement_bucket', 'settled_date']
        aliases = {group: 'g_' + group for group in all_groups}
        metrics = self._metric_expressions()
        # GROUPING SETS needs a sort for each COUNT(DISTINCT match_id) group.
        # A bounded local work_mem prevents the default 4MB setting from
        # repeatedly spilling this compact history projection to disk.
        self.connection.execute("SET LOCAL work_mem='64MB'").close()
        source = ('SELECT *, ' + ','.join(self._group_expression(group) + ' AS ' + aliases[group]
                                       for group in all_groups) + ' FROM ' + self._statistics_table() + ' WHERE ' + where)
        sql = ('WITH source AS (' + source + ') SELECT ' +
               ','.join('grouping(' + alias + ') AS mask_' + group + ',' + alias + ' AS bucket_' + group
                        for group, alias in aliases.items()) + ',' +
               ','.join(value + ' AS ' + key for key, value in metrics.items()) +
               ' FROM source GROUP BY GROUPING SETS ((),' +
               ','.join('(' + aliases[group] + ')' for group in all_groups[:-1]) + ',' +
               '(' + aliases['settlement_bucket'] + ',' + aliases['settled_date'] + '))')
        out: dict[str, Any] = {group: {} for group in groups}
        out.update(settled={}, settled_by_date={})
        for raw in self.connection.execute(sql, params):
            row = dict(raw)
            present = [group for group in all_groups if row['mask_' + group] == 0]
            stats = self._finish_aggregate({key: row[key] for key in metrics}, metrics)
            if not present:
                out['overall'] = stats
            elif present == ['settlement_bucket']:
                if row['bucket_settlement_bucket'] == 'graded':
                    out['settled'] = stats
            elif present == ['settlement_bucket', 'settled_date']:
                if row['bucket_settlement_bucket'] == 'graded':
                    out['settled_by_date'][str(row['bucket_settled_date'])] = stats
            else:
                out[present[0]][str(row['bucket_' + present[0]])] = stats
        if not out['settled']:
            out['settled'] = self._finish_aggregate({key: None for key in metrics}, metrics)
        return out

    def evidence(self, cutoff: str) -> list:
        # Each match contributes one independent observation per algorithm.
        sql = """SELECT algorithm, match_id, count(*) AS quotes,
          avg(CASE status WHEN 'won' THEN 1.0 WHEN 'half_won' THEN 0.5 ELSE 0 END) AS wins,
          avg(CASE status WHEN 'lost' THEN 1.0 WHEN 'half_lost' THEN 0.5 ELSE 0 END) AS losses,
          sum((p_fused-(CASE WHEN status IN ('won','half_won') THEN 1.0 ELSE 0.0 END))*(p_fused-(CASE WHEN status IN ('won','half_won') THEN 1.0 ELSE 0.0 END))*
              CASE WHEN status IN ('half_won','half_lost') THEN .5 ELSE 1 END) /
              sum(CASE WHEN status IN ('half_won','half_lost') THEN .5 ELSE 1 END) AS brier,
          sum((p_market-(CASE WHEN status IN ('won','half_won') THEN 1.0 ELSE 0.0 END))*(p_market-(CASE WHEN status IN ('won','half_won') THEN 1.0 ELSE 0.0 END))*
              CASE WHEN status IN ('half_won','half_lost') THEN .5 ELSE 1 END) /
              sum(CASE WHEN status IN ('half_won','half_lost') THEN .5 ELSE 1 END) AS market_brier
          FROM """ + self._statistics_table() + """ WHERE competition_type='real' AND trigger='live_forecast'
          AND model_version=?
          AND research_only=0 AND """ + CLASSIFIED + """ AND settled_at<=? AND at<settled_at
          AND p_fused>0 AND p_market>0 GROUP BY algorithm,match_id"""
        return [dict(r) for r in self.connection.execute(sql, (MODEL_VERSION, cutoff))]
