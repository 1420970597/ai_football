"""Indexed SQLite projection of the append-only decision audit journal.

Imports are incremental and transactional. Readers never parse the audit log or
deserialize unbounded entry payloads to compute statistics.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence


FIELDS = {
    'match_id': 'TEXT', 'at': 'TEXT', 'algorithm': 'TEXT', 'competition_type': 'TEXT',
    'trigger': 'TEXT', 'decision': 'TEXT', 'market': 'TEXT', 'league': 'TEXT',
    'config_version': 'INTEGER', 'status': 'TEXT', 'is_pick': 'INTEGER',
    'is_live': 'INTEGER', 'odds': 'REAL', 'pnl': 'REAL', 'confidence': 'REAL',
    'kelly': 'REAL', 'p_fused': 'REAL', 'p_market': 'REAL', 'closing_odds': 'REAL',
    'settled_at': 'TEXT', 'decision_id': 'TEXT', 'experiment_id': 'TEXT',
    'review_status': 'TEXT', 'research_only': 'INTEGER',
}
GRADED = "status IN ('won','lost','half_won','half_lost','push')"
CLASSIFIED = "status IN ('won','lost','half_won','half_lost')"


class LedgerDB:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA synchronous=FULL')
        self.connection.execute('PRAGMA busy_timeout=10000')
        columns = ','.join(k + ' ' + v for k, v in FIELDS.items())
        self.connection.execute('CREATE TABLE IF NOT EXISTS decisions '
                                '(identity TEXT PRIMARY KEY, revision REAL, payload TEXT,' + columns + ')')
        for name, cols in {
            'history': 'competition_type,trigger,algorithm,at',
            'pending': 'status,match_id', 'date': 'at',
            'performance': 'competition_type,trigger,status,algorithm,settled_at',
            'experiment': 'experiment_id,algorithm',
        }.items():
            self.connection.execute('CREATE INDEX IF NOT EXISTS idx_' + name + ' ON decisions(' + cols + ')')
        self.connection.execute('CREATE TABLE IF NOT EXISTS imports '
                                '(path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER)')
        self.connection.commit()

    def __del__(self) -> None:
        self.connection.close()

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

    def entries(self, where: str = '1', params: Sequence[Any] = (),
                limit: int | None = None, offset: int = 0) -> list:
        sql = 'SELECT payload FROM decisions WHERE ' + where
        sql += ' ORDER BY (status=\'pending\'),at DESC,identity'
        if limit is not None:
            sql += ' LIMIT ? OFFSET ?'
            params = (*params, limit, offset)
        return [json.loads(r[0]) for r in self.connection.execute(sql, params)]

    def aggregate(self, where: str = '1', params: Sequence[Any] = (),
                  group: str | None = None) -> Dict[str, Any]:
        if group and group not in {*FIELDS, 'date_key', 'confidence_band'}:
            raise ValueError('invalid aggregation')
        expression = ("substr(at,1,10)" if group == 'date_key' else
                      "CASE WHEN confidence>=0.8 THEN '80–100%' WHEN confidence>=0.6 THEN '60–80%' "
                      "WHEN confidence>0 THEN '0–60%' ELSE '未记录' END" if group == 'confidence_band' else group)
        staked = '(' + GRADED + ' AND is_pick=1)'
        nonpush = '(' + CLASSIFIED + ' AND is_pick=1)'
        win = "CASE status WHEN 'won' THEN 1.0 WHEN 'half_won' THEN 0.5 ELSE 0 END"
        loss = "CASE status WHEN 'lost' THEN 1.0 WHEN 'half_lost' THEN 0.5 ELSE 0 END"
        metrics = {
            'total': 'count(*)', 'matches': 'count(DISTINCT match_id)',
            'legacy_identity_rows': "sum(decision_id='')",
            **{s: "sum(status='" + s + "')" for s in
               ('pending', 'void')},
            'graded': 'sum(' + GRADED + ')',
            **{s:"sum(is_pick=1 AND status='"+s+"')" for s in ('half_won','half_lost','push')},
            'won': "sum(is_pick=1 AND status IN ('won','half_won'))",
            'lost': "sum(is_pick=1 AND status IN ('lost','half_lost'))",
            'win_stake_units': 'sum(CASE WHEN is_pick=1 THEN ' + win + ' ELSE 0 END)',
            'loss_stake_units': 'sum(CASE WHEN is_pick=1 THEN ' + loss + ' ELSE 0 END)',
            'stake_units': 'sum(' + staked + ')',
            'profit_units': 'sum(CASE WHEN ' + staked + ' THEN pnl ELSE 0 END)',
            'avg_odds': 'avg(CASE WHEN ' + staked + ' THEN odds END)',
            'confidence_stake': 'sum(CASE WHEN ' + staked + ' THEN confidence ELSE 0 END)',
            'confidence_profit': 'sum(CASE WHEN ' + staked + ' THEN pnl*confidence ELSE 0 END)',
            'allocated_stake': 'sum(CASE WHEN ' + staked + ' THEN kelly ELSE 0 END)',
            'allocated_profit': 'sum(CASE WHEN ' + staked + ' THEN pnl*kelly ELSE 0 END)',
            'direction_samples': "sum(" + nonpush + " AND market IN ('HAD','HAD_1H'))",
            'direction_correct': "sum(is_pick=1 AND status='won' AND market IN ('HAD','HAD_1H'))",
            'clv_n': 'sum(' + staked + ' AND is_live=0 AND closing_odds>1 AND odds>1)',
            'clv_mean': 'avg(CASE WHEN ' + staked + ' AND is_live=0 AND closing_odds>1 AND odds>1 THEN odds/closing_odds-1 END)',
            'clv_positive_rate': 'avg(CASE WHEN ' + staked + ' AND is_live=0 AND closing_odds>1 AND odds>1 THEN (odds>closing_odds)*1.0 END)',
            'unpicked_n': 'sum(' + GRADED + ' AND is_pick=0)',
            'unpicked_wins': "sum(is_pick=0 AND status IN ('won','half_won'))",
            'unpicked_losses': "sum(is_pick=0 AND status IN ('lost','half_lost'))",
        }
        sql = 'SELECT ' + ((str(expression) + ' AS bucket,') if group else '')
        sql += ','.join(v + ' AS ' + k for k, v in metrics.items()) + ' FROM decisions WHERE ' + where
        if group:
            sql += ' GROUP BY ' + str(expression)
        rows = [dict(r) for r in self.connection.execute(sql, params)]
        def finish(r: Dict[str, Any]) -> Dict[str, Any]:
            for key in metrics:
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
        result = [finish(r) for r in rows]
        return {str(r.pop('bucket')): r for r in result} if group else result[0]

    def evidence(self, cutoff: str) -> list:
        # Each match contributes one independent observation per algorithm.
        sql = """SELECT algorithm, match_id, count(*) AS quotes,
          avg(CASE status WHEN 'won' THEN 1.0 WHEN 'half_won' THEN 0.5 ELSE 0 END) AS wins,
          avg(CASE status WHEN 'lost' THEN 1.0 WHEN 'half_lost' THEN 0.5 ELSE 0 END) AS losses,
          avg((p_fused-(status IN ('won','half_won')))*(p_fused-(status IN ('won','half_won')))) AS brier,
          avg((p_market-(status IN ('won','half_won')))*(p_market-(status IN ('won','half_won')))) AS market_brier
          FROM decisions WHERE competition_type='real' AND trigger='live_forecast'
          AND research_only=0 AND """ + CLASSIFIED + """ AND settled_at<=? AND at<settled_at
          AND p_fused>0 AND p_market>0 GROUP BY algorithm,match_id"""
        return [dict(r) for r in self.connection.execute(sql, (cutoff,))]
