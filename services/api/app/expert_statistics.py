"""统计只采用可复核报价、赛果及计算台账；当前投影可以重建。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException, Query


def metrics(row: dict) -> dict:
    value = dict(row)
    settled = sum(value.get(key, 0) for key in ('win', 'half_win', 'loss', 'half_loss', 'push', 'void'))
    effective = Decimal(str(value.get('effective_stake') or 0))
    net = Decimal(str(value.get('unit_net') or 0))
    win_units = Decimal(value.get('win', 0)) + Decimal(value.get('half_win', 0)) / 2
    value.update(settled_signals=settled, winning_signals=value.get('win', 0) + value.get('half_win', 0),
                 accuracy=float(win_units / effective) if effective else None,
                 average_unit_net=float(net / settled) if settled else None,
                 unit_roi=float(net / effective) if effective else None,
                 unit_net=float(net), effective_stake=float(effective),
                 sample_state='no_settled_evidence' if not settled else 'limited_sample' if settled < 30 else 'observed_sample')
    return value


# Kept for the existing standalone benchmark; production uses classified signals.
METRIC_SQL = (
    "COUNT(s.id) AS total_signals,COUNT(s.id) FILTER(WHERE s.outcome='pending') AS pending,"
    + ','.join(f"COUNT(s.id) FILTER(WHERE s.outcome='{name}') AS {name}" for name in ('win','half_win','loss','half_loss','push','void'))
    + ",COALESCE(SUM(s.unit_net) FILTER(WHERE s.outcome<>'pending'),0) AS unit_net,"
    "COALESCE(SUM(s.effective_stake) FILTER(WHERE s.outcome<>'pending'),0) AS effective_stake"
)
QUALITY_METRIC_SQL = (
    "COUNT(s.id) AS total_signals,COUNT(s.id) FILTER(WHERE s.statistics_eligible) AS eligible_signals,"
    "COUNT(s.id) FILTER(WHERE NOT s.statistics_eligible) AS excluded_signals,"
    "COUNT(s.id) FILTER(WHERE NOT s.quote_valid) AS missing_quote_evidence,"
    "COUNT(s.id) FILTER(WHERE s.quote_valid AND s.outcome<>'pending' AND NOT s.settlement_valid) AS invalid_settlement_evidence,"
    "COUNT(s.id) FILTER(WHERE s.statistics_eligible AND s.outcome='pending') AS pending,"
    + ','.join(f"COUNT(s.id) FILTER(WHERE s.statistics_eligible AND s.outcome='{name}') AS {name}" for name in ('win','half_win','loss','half_loss','push','void'))
    + ",COALESCE(SUM(s.unit_net) FILTER(WHERE s.statistics_eligible AND s.outcome<>'pending'),0) AS unit_net,"
    "COALESCE(SUM(s.effective_stake) FILTER(WHERE s.statistics_eligible AND s.outcome<>'pending'),0) AS effective_stake"
)
QUOTE_VALID_SQL = (
    "EXISTS(SELECT 1 FROM v2_quote_evidence q WHERE q.evidence_kind='expert_signal' AND q.reference_id=s.id "
    "AND q.fixture_id=s.fixture_id AND q.market_id=s.market_id AND q.period=s.period AND q.line=s.line "
    "AND q.selection=s.selection AND q.price=s.recommendation_price AND q.source_updated_at=s.quote_time "
    "AND q.score_baseline IS NOT DISTINCT FROM s.score_baseline "
    "AND q.evidence->>'score_basis'=s.score_basis)"
)
SETTLEMENT_VALID_SQL = (
    "s.settled_at IS NOT NULL AND EXISTS(SELECT 1 FROM v2_signal_settlements l "
    "JOIN v2_fixture_results r ON r.fixture_id=l.fixture_id AND r.version=l.result_version "
    "WHERE l.signal_id=s.id AND l.fixture_id=s.fixture_id AND l.result_version=s.result_version "
    "AND l.outcome=s.outcome AND l.unit_net IS NOT DISTINCT FROM s.unit_net "
    "AND l.effective_stake=s.effective_stake AND l.reason=s.settlement_reason "
    "AND r.status IN ('confirmed','void') AND r.version=(SELECT max(x.version) FROM v2_fixture_results x WHERE x.fixture_id=s.fixture_id))"
)
DEFINITIONS = {
    'accuracy': '(全赢数量 + 半赢数量 × 0.5) ÷ (全赢 + 全输 + 半赢 × 0.5 + 半输 × 0.5)，走盘和作废不入分母',
    'unit_net': '每条推荐按本金1与推荐时系统赔率结算后的净收益之和',
    'average_unit_net': '一单位净收益合计 ÷ 已结算推荐数量（含走盘和作废）',
    'unit_roi': '一单位净收益合计 ÷ 有效结算本金（半赢半输计0.5，走盘作废计0）',
    'scope': '专家推荐统计，与真实账户成交收益分开；撤回记录保留证据并继续结算',
    'time_basis': '已结算按最新结算时间，待结算按推荐创建时间；所有窗口为UTC左闭右开',
    'quality': '收益只含不可变系统报价及最新赛果计算台账一致的记录，缺失或损坏记录单列排除，不补零',
    'sample_state': '少于30条已结算记录标为有限样本；30条以上也不代表未来盈利或统计显著性',
}


@dataclass(frozen=True)
class StatisticsScope:
    start: datetime
    end: datetime
    source_id: uuid.UUID | None = None
    phase: str = 'all'
    market_kind: str = 'all'

    def response(self):
        return {'from_utc': self.start, 'to_utc': self.end,
                'filters': {'source_id': str(self.source_id) if self.source_id else None,
                            'phase': self.phase, 'market_kind': self.market_kind},
                'time_basis': 'latest_settlement_at; pending_created_at', 'definitions': DEFINITIONS}


def statistics_scope(days: int = Query(default=30, ge=1, le=3650),
                     source_id: uuid.UUID | None = None,
                     phase: str = Query(default='all', pattern='^(all|scheduled|live)$'),
                     market_kind: str = Query(default='all', pattern='^(all|winner|handicap|total)$'),
                     from_utc: datetime | None = None, to_utc: datetime | None = None):
    if any(value is not None and (value.tzinfo is None or value.utcoffset() is None) for value in (from_utc, to_utc)):
        raise HTTPException(422, '统计时间必须包含时区')
    end = to_utc.astimezone(timezone.utc) if to_utc else datetime.now(timezone.utc)
    start = from_utc.astimezone(timezone.utc) if from_utc else end - timedelta(days=days)
    if start >= end or end-start > timedelta(days=3650):
        raise HTTPException(422, '统计时间范围必须递增且不超过3650天')
    return StatisticsScope(start, end, source_id, phase, market_kind)


def signal_scope(scope: StatisticsScope, expert_id=None, *, classify=True):
    args = [scope.start, scope.end, scope.source_id, scope.phase, scope.market_kind, expert_id]
    columns = ',' + QUOTE_VALID_SQL + ' AS quote_valid,(' + SETTLEMENT_VALID_SQL + ') AS settlement_valid' if classify else ''
    opening = 'WITH checked AS MATERIALIZED (' if classify else 'WITH scoped AS ('
    query = (
        opening + 'SELECT s.*' + columns + ' '
        'FROM v2_expert_signals s WHERE '
        "(CASE WHEN s.outcome='pending' THEN s.created_at ELSE COALESCE(s.settled_at,s.created_at) END)>=$1 "
        "AND (CASE WHEN s.outcome='pending' THEN s.created_at ELSE COALESCE(s.settled_at,s.created_at) END)<$2 "
        'AND ($3::uuid IS NULL OR EXISTS(SELECT 1 FROM v2_expert_messages m WHERE m.id=s.message_id AND m.source_id=$3)) '
        "AND ($4::text='all' OR s.phase=$4) AND ($5::text='all' OR s.market_kind=$5) "
        'AND ($6::uuid IS NULL OR s.expert_id=$6)) '
    )
    if classify:
        query += ",scoped AS (SELECT checked.*,quote_valid AND (outcome='pending' OR settlement_valid) AS statistics_eligible FROM checked) "
    return query, args


async def summaries(pool, start: datetime, end: datetime, *, scope: StatisticsScope | None = None, expert_id=None):
    scope = scope or StatisticsScope(start, end)
    prefix, args = signal_scope(scope, expert_id)
    rows = await pool.fetch(prefix + 'SELECT e.id,e.display_nickname,e.avatar_object_key,e.enabled,' + QUALITY_METRIC_SQL +
        ' FROM v2_experts e LEFT JOIN scoped s ON s.expert_id=e.id '
        'WHERE e.enabled AND ($6::uuid IS NULL OR e.id=$6) GROUP BY e.id '
        'ORDER BY unit_net DESC,total_signals DESC,e.display_nickname,e.id', *args)
    return [metrics(dict(row)) for row in rows]


async def trend(pool, expert_id, scope: StatisticsScope):
    prefix, args = signal_scope(scope, expert_id)
    rows = await pool.fetch(prefix +
        ",daily AS (SELECT date_trunc('day',settled_at AT TIME ZONE 'UTC') AS day,"
        "SUM(unit_net) AS net,COUNT(*) AS settled,SUM(effective_stake) AS effective "
        "FROM scoped WHERE statistics_eligible AND outcome<>'pending' GROUP BY 1) "
        "SELECT day,net,settled,effective,SUM(net) OVER(ORDER BY day) AS cumulative_unit_net,"
        "SUM(settled) OVER(ORDER BY day) AS cumulative_settled,"
        "SUM(net) OVER(ORDER BY day)/SUM(settled) OVER(ORDER BY day) AS average_unit_net,"
        "SUM(net) OVER(ORDER BY day)/NULLIF(SUM(effective) OVER(ORDER BY day),0) AS unit_roi FROM daily ORDER BY day", *args)
    return [dict(row) for row in rows]


async def source_options(pool, scope: StatisticsScope):
    prefix, args = signal_scope(replace(scope, source_id=None), classify=False)
    rows = await pool.fetch(prefix +
        'SELECT DISTINCT x.id,x.display_name FROM v2_expert_sources x '
        'JOIN v2_expert_messages m ON m.source_id=x.id JOIN scoped s ON s.message_id=m.id '
        'JOIN v2_experts e ON e.id=s.expert_id WHERE x.enabled AND e.enabled ORDER BY x.display_name,x.id', *args)
    return [dict(row) for row in rows]
