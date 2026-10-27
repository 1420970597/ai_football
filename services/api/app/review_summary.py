"""Deterministic Chinese receipt based only on committed system evidence."""
from __future__ import annotations

from collections import Counter
from datetime import datetime

from .review_state import trace

SUMMARY_ITEM_LIMIT = 100
PHASES = {'scheduled': '未开赛', 'live': '滚球', 'finished': '已完场', 'cancelled': '取消或延期'}
PERIODS = {'full_time': '全场', 'first_half': '上半场', 'second_half': '下半场'}
SELECTIONS = {'home': '主队', 'away': '客队', 'draw': '平局', 'over': '大', 'under': '小',
              'yes': '是', 'no': '否', 'odd': '单', 'even': '双'}
REASONS = {
    'quote_not_read_or_changed': '盘口未读取或报价已经变化',
    'quote_or_score_context_changed': '盘口或比分基线已经变化',
    'fixture_not_active': '比赛当前不可推荐',
    'quote_inactive_unknown_or_stale': '盘口停用、源时间未知或报价过期',
    'historical_review_not_current': '历史消息仅保留分析，不生成实时推荐',
    'message_timestamp_in_future': '消息发布时间晚于当前时间',
}


def _value(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def make_summary(rows, *, accepted_total: int, rejected, review_mode: str) -> dict:
    """Model prose and Telegram prices do not participate in the system receipt."""
    items = [{key: _value(row.get(key)) for key in (
        'signal_id', 'fixture_id', 'home_team', 'away_team', 'competition_name',
        'kickoff_at', 'phase', 'market_name', 'market_kind', 'period', 'line',
        'selection', 'recommendation_price', 'quote_time',
    )} for row in rows[:SUMMARY_ITEM_LIMIT]]
    omitted = max(0, accepted_total - len(items))
    reasons = Counter()
    for entry in rejected:
        code = entry.get('reason') if isinstance(entry, dict) else None
        reasons[code if isinstance(code, str) and code in REASONS else 'unverified_rejection'] += 1
    lines = [f'系统已接受 {accepted_total} 条推荐。' if accepted_total else '系统尚未接受任何推荐，本条消息不计入收益，也不触发自动投注。']
    if review_mode == 'historical_replay':
        lines.append('审核模式：历史回放；当前盘口不能替代历史推荐报价。')
    for number, item in enumerate(items, 1):
        line = '不适用' if item['market_kind'] == 'winner' and item['line'] is None else item['line'] or '未知'
        selection = SELECTIONS.get(item['selection'], '系统选项：' + str(item['selection']))
        phase = PHASES.get(item['phase'], '阶段未知')
        period = PERIODS.get(item['period'], '系统期间：' + str(item['period']))
        lines.append(f"{number}. {item['home_team']} 对 {item['away_team']}；{item['competition_name']}；"
                     f"{phase}；{period}；{item['market_name']}，盘口线 {line}，方向 {selection}；"
                     f"系统赔率 {item['recommendation_price']}。")
    if omitted:
        lines.append(f'另有 {omitted} 条已接受记录，请查看对应系统推荐明细；本摘要仅展示前 {SUMMARY_ITEM_LIMIT} 条。')
    for code, count in sorted(reasons.items()):
        lines.append(f"未解决：{REASONS.get(code, '工具未确认，保留原始轨迹核验')}（{count} 项工具拒绝记录，可能含重复尝试）。")
    if not items and not reasons:
        lines.append('本次工具尚无已确认的盘口录入；模型原始说明仅供核查。')
    return {'schema_version': 1, 'language': 'zh-CN', 'basis': 'committed_system_signals',
            'review_mode': review_mode, 'accepted_count': accepted_total, 'items': items,
            'omitted_count': omitted, 'tool_rejection_counts': dict(reasons), 'content': chr(10).join(lines)}


async def record_summary(conn, job, attempt_id, review_mode: str) -> int:
    """Caller holds the active job lock in the same completion transaction."""
    total = await conn.fetchval('SELECT count(*) FROM v2_expert_signals WHERE message_id=$1', job['message_id'])
    rows = await conn.fetch(
        'SELECT s.id AS signal_id,s.fixture_id,f.home_team,f.away_team,f.competition_name,f.kickoff_at,'
        's.phase,s.market_name,s.market_kind,s.period,s.line,s.selection,s.recommendation_price,s.quote_time '
        'FROM v2_expert_signals s JOIN v2_fixtures f ON f.id=s.fixture_id WHERE s.message_id=$1 '
        'ORDER BY s.created_at,s.id LIMIT $2', job['message_id'], SUMMARY_ITEM_LIMIT)
    records = await conn.fetch(
        "SELECT payload->'rejected' AS rejected FROM v2_analysis_traces WHERE job_id=$1 "
        "AND trace_kind='mcp_record_expert_signals' AND payload->>'attempt_id'=$2 ORDER BY sequence_no",
        job['id'], str(attempt_id))
    rejected = [item for record in records for item in (record['rejected'] or [])]
    receipt = make_summary([dict(row) for row in rows], accepted_total=total, rejected=rejected, review_mode=review_mode)
    await trace(conn, job['id'], 'system_review_result', {'attempt_id': str(attempt_id), **receipt})
    return total
