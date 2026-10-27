"""统计以推荐时系统价格和最新有证据结算为准。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal


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
                 unit_net=float(net), effective_stake=float(effective))
    return value


METRIC_SQL = (
    "COUNT(s.id) AS total_signals,COUNT(s.id) FILTER(WHERE s.outcome='pending') AS pending,"
    + ','.join(f"COUNT(s.id) FILTER(WHERE s.outcome='{name}') AS {name}" for name in ('win','half_win','loss','half_loss','push','void'))
    + ",COALESCE(SUM(s.unit_net) FILTER(WHERE s.outcome<>'pending'),0) AS unit_net,"
    "COALESCE(SUM(s.effective_stake) FILTER(WHERE s.outcome<>'pending'),0) AS effective_stake"
)
DEFINITIONS = {
    'accuracy': '(全赢数量 + 半赢数量 × 0.5) ÷ (全赢 + 全输 + 半赢 × 0.5 + 半输 × 0.5)，走盘和作废不入分母',
    'unit_net': '每条推荐按本金1与推荐时系统赔率结算后的净收益之和',
    'average_unit_net': '一单位净收益合计 ÷ 已结算推荐数量（含走盘和作废）',
    'unit_roi': '一单位净收益合计 ÷ 有效结算本金（半赢半输计0.5，走盘作废计0）',
    'scope': '专家推荐统计，与真实账户成交收益分开；撤回记录保留证据并继续结算',
}


async def summaries(pool, start: datetime, end: datetime):
    rows = await pool.fetch(
        'SELECT e.id,e.display_nickname,e.avatar_object_key,e.enabled,' + METRIC_SQL +
        ' FROM v2_experts e LEFT JOIN v2_expert_signals s ON s.expert_id=e.id AND s.created_at>=$1 AND s.created_at<$2 '
        'WHERE e.enabled GROUP BY e.id ORDER BY unit_net DESC,total_signals DESC,e.display_nickname,e.id', start, end,
    )
    return [metrics(dict(row)) for row in rows]
