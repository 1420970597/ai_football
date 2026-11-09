"""One evidence-based, fixed-window definition shared by dispatch and send guards."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException

from .expert_statistics import StatisticsScope, signal_scope

PROFITABILITY_DAYS = 30
EFFECTIVE_OUTCOMES = ('win', 'half_win', 'loss', 'half_loss')
PROFITABILITY_DEFINITION = {
    'days': PROFITABILITY_DAYS,
    'time_basis': 'latest_settlement_at; active_statistics_cycle',
    'required_unit_net': '>0',
    'required_effective_settled_signals': '>0',
    'neutral_outcomes_excluded': ['pending', 'push', 'void'],
}


def profitable(row) -> bool:
    if row is None or row['effective_settled_signals'] <= 0:
        return False
    net = Decimal(str(row['unit_net']))
    return net.is_finite() and net > 0


async def profitability_rows(conn, *, expert_id=None, at=None):
    end = at or datetime.now(timezone.utc)
    prefix, args = signal_scope(StatisticsScope(end-timedelta(days=PROFITABILITY_DAYS), end), expert_id)
    return await conn.fetch(prefix +
        "SELECT expert_id,COUNT(*) FILTER(WHERE outcome IN('win','half_win','loss','half_loss')) AS effective_settled_signals,"
        "COALESCE(SUM(unit_net),0) AS unit_net FROM scoped "
        "WHERE statistics_eligible AND outcome<>'pending' GROUP BY expert_id", *args)


async def require_expert_permission(conn, policy, expert_id):
    mode = policy.get('expert_selection_mode', 'manual')
    if mode == 'manual':
        if expert_id not in policy['allowed_experts']:
            raise HTTPException(403, '本人未允许此专家自动投注')
        return
    if mode != 'profitable':
        raise HTTPException(409, '专家选择模式无效')
    rows = await profitability_rows(conn, expert_id=expert_id)
    if not rows or not profitable(rows[0]):
        raise HTTPException(409, '专家近30日当前统计周期须一单位净收益大于0且有效结算大于0')
