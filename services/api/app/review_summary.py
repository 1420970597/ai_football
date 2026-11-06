"""Deterministic Chinese receipt based only on committed system evidence."""
from __future__ import annotations

from collections import Counter
from datetime import datetime

from .review_state import append_traces, trace
from .handicap_lines import line_terms
from .review_completion import completion_issues, mentioned_fixtures
from .review_summary_evidence import read_summary_evidence

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


def make_summary(rows, *, accepted_total: int, rejected, review_mode: str, identified=(), identified_total=None,
                 incomplete=None) -> dict:
    """Model prose and Telegram prices do not participate in the system receipt."""
    items = [{key: _value(row.get(key)) for key in (
        'signal_id', 'fixture_id', 'home_team', 'away_team', 'competition_name',
        'kickoff_at', 'phase', 'market_name', 'market_kind', 'period', 'line',
        'selection', 'recommendation_price', 'quote_time',
    )} for row in rows[:SUMMARY_ITEM_LIMIT]]
    for item, row in zip(items, rows):
        item.update({key: _value(value) for key, value in line_terms(row).items()})
    omitted = max(0, accepted_total - len(items))
    identified_items = [{key: _value(row.get(key)) for key in (
        'fixture_id', 'home_team', 'away_team', 'competition_name', 'kickoff_at', 'phase',
    )} for row in identified[:SUMMARY_ITEM_LIMIT]]
    identified_omitted = max(0, (len(identified) if identified_total is None else identified_total) - len(identified_items))
    reasons = Counter()
    for entry in rejected:
        code = entry.get('reason') if isinstance(entry, dict) else None
        reasons[code if isinstance(code, str) and code in REASONS else 'unverified_rejection'] += 1
    lines = [f'系统已接受 {accepted_total} 条推荐。' if accepted_total else '系统尚未接受任何推荐，本条消息不计入收益，也不触发自动投注。']
    if review_mode == 'historical_replay':
        lines.append('审核模式：历史回放；当前盘口不能替代历史推荐报价。')
    for number, item in enumerate(items, 1):
        line = '不适用' if item['market_kind'] == 'winner' and item['selection_line'] is None else item['selection_line'] or '未知'
        selection = SELECTIONS.get(item['selection'], '系统选项：' + str(item['selection']))
        phase = PHASES.get(item['phase'], '阶段未知')
        period = PERIODS.get(item['period'], '系统期间：' + str(item['period']))
        lines.append(f"{number}. {item['home_team']} 对 {item['away_team']}；{item['competition_name']}；"
                     f"{phase}；{period}；{item['market_name']}，盘口线 {line}，方向 {selection}；"
                     f"系统赔率 {item['recommendation_price']}。")
    if omitted:
        lines.append(f'另有 {omitted} 条已接受记录，请查看对应系统推荐明细；本摘要仅展示前 {SUMMARY_ITEM_LIMIT} 条。')
    for item in identified_items:
        phase = PHASES.get(item['phase'], '阶段未知')
        reason = ('当前尚无工具接受的盘口推荐' if item['phase'] in {'scheduled', 'live'}
                  else '当前阶段不允许读取实时盘口或录入推荐')
        lines.append(f"已唯一识别但未进入实时推荐：{item['home_team']} 对 {item['away_team']}；"
                     f"{item['competition_name']}；{phase}；{reason}。")
    if identified_omitted:
        lines.append(f'另有 {identified_omitted} 场已识别但未录入推荐的比赛；本摘要仅展示前 {SUMMARY_ITEM_LIMIT} 场。')
    for code, count in sorted(reasons.items()):
        lines.append(f"未解决：{REASONS.get(code, '工具未确认，保留原始轨迹核验')}（{count} 项工具拒绝记录，可能含重复尝试）。")
    if not items and not reasons and not identified_items:
        lines.append('本次工具尚无已确认的盘口录入；模型原始说明仅供核查。')
    if incomplete:
        lines.append(f"审核核验未完成：{incomplete['unfinished_search_count']} 个检索尚未读取全部分页，"
                     f"{incomplete['unbound_mention_count']} 个模型提及的比赛编号没有当前工具绑定；"
                     '已达到本次续审上限，不能认定全部匹配。仅已接受记录有效，其余保留未解决。')
    return {'schema_version': 1, 'language': 'zh-CN', 'basis': 'committed_system_signals',
            'review_mode': review_mode, 'accepted_count': accepted_total, 'items': items,
            'identified_without_signal': identified_items, 'identified_omitted_count': identified_omitted,
            'completion_verified': not incomplete, 'completion_issues': incomplete or {},
            'omitted_count': omitted, 'tool_rejection_counts': dict(reasons), 'content': chr(10).join(lines)}


async def record_summary(conn, job, attempt_id, review_mode: str, *, trace_lock_held: bool = False) -> int:
    """Caller holds the active job lock in the same completion transaction."""
    evidence = await read_summary_evidence(conn, job['id'], attempt_id, job['message_id'], SUMMARY_ITEM_LIMIT)
    mentioned = set().union(*(mentioned_fixtures(content) for content in evidence.model_contents))
    incomplete = (await completion_issues(conn, job['id'], attempt_id, mentioned)
                  if mentioned else evidence.pagination_issues)
    receipt = make_summary(evidence.signals, accepted_total=evidence.total, rejected=evidence.rejected, review_mode=review_mode,
                           identified=evidence.identified, identified_total=evidence.identified[0]['total'] if evidence.identified else 0,
                           incomplete=incomplete)
    payload = {'attempt_id': str(attempt_id), **receipt}
    if trace_lock_held:
        await append_traces(conn, job['id'], [('system_review_result', payload)], trace_lock_held=True)
    else:
        await trace(conn, job['id'], 'system_review_result', payload)
    return evidence.total
