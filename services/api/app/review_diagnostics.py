"""Read-only explanations, never evidence authorizing a recommendation."""
from __future__ import annotations

LABELS = {
    'source_edited': '原版本已被编辑替代', 'source_deleted': '原消息已删除',
    'deadline_with_signals': '已形成推荐，剩余审核超时',
    'deadline_without_signals': '20秒内未形成推荐',
    'accepted': '已形成推荐', 'verification_incomplete': '推荐核验未完成',
    'model_concluded_without_signal': '未形成推荐·查看分析',
    'market_unavailable': '已读盘口·未形成推荐', 'identity_unresolved': '已检索·身份或意图待核验',
    'failed': '审核执行失败', 'pending': '审核处理中',
    'unresolved': '尚未形成已确认推荐', 'not_reviewed': '尚无审核任务',
}


def diagnosis(row):
    state, error = row.get('state'), row.get('last_error')
    accepted = int(row.get('accepted_signal_count') or 0)
    summary = row.get('summary') or {}
    if error in {'source_edited', 'source_deleted'}:
        code = error
    elif error == 'detection_deadline_exceeded':
        code = 'deadline_with_signals' if accepted else 'deadline_without_signals'
    elif state in {'queued', 'running', 'retry'}:
        code = 'pending'
    elif state == 'failed':
        code = 'failed'
    elif accepted:
        code = 'accepted'
    elif summary.get('completion_issues'):
        code = 'verification_incomplete'
    elif row.get('has_market_reads'):
        code = 'market_unavailable'
    elif row.get('has_searches'):
        code = 'identity_unresolved'
    elif row.get('has_model_result') and summary.get('completion_verified') is True:
        # This is a model conclusion, not proof of absence of an intention.
        code = 'model_concluded_without_signal'
    else:
        code = 'unresolved' if state else 'not_reviewed'
    return {'code': code, 'label': LABELS[code], 'accepted_signal_count': accepted,
            'completion_verified': summary.get('completion_verified') is True,
            'requires_attention': code in {'failed','deadline_without_signals',
                'deadline_with_signals','verification_incomplete','identity_unresolved','market_unavailable','unresolved','not_reviewed'},
            'model_conclusion_is_not_acceptance': True}


async def message_diagnostics(pool, message_ids):
    if not message_ids:
        return {}
    rows = await pool.fetch(
        "WITH ids AS (SELECT unnest($1::uuid[]) id) "
        "SELECT ids.id,j.state,j.last_error,receipt.payload AS summary,"
        "(SELECT count(*) FROM v2_expert_signals s WHERE s.message_id=ids.id) accepted_signal_count,"
        "EXISTS(SELECT 1 FROM v2_analysis_traces t WHERE t.job_id=j.id AND "
        "t.payload->>'attempt_id'=j.attempt_id::text AND trace_kind='mcp_search_matches') has_searches,"
        "EXISTS(SELECT 1 FROM v2_analysis_traces t WHERE t.job_id=j.id AND "
        "t.payload->>'attempt_id'=j.attempt_id::text AND trace_kind='mcp_list_match_markets') has_market_reads,"
        "EXISTS(SELECT 1 FROM v2_analysis_traces t WHERE t.job_id=j.id AND "
        "t.payload->>'attempt_id'=j.attempt_id::text AND trace_kind='hermes_result') has_model_result "
        "FROM ids LEFT JOIN LATERAL(SELECT id,state,last_error,attempt_id FROM v2_analysis_jobs "
        "WHERE message_id=ids.id ORDER BY created_at DESC,id DESC LIMIT 1)j ON TRUE "
        "LEFT JOIN LATERAL(SELECT payload FROM v2_analysis_traces t WHERE t.job_id=j.id "
        "AND trace_kind='system_review_result' AND t.payload->>'attempt_id'=j.attempt_id::text "
        "ORDER BY sequence_no DESC LIMIT 1)receipt ON TRUE", list(dict.fromkeys(message_ids)))
    return {row['id']: diagnosis(dict(row)) for row in rows}


async def annotate_messages(pool, rows):
    values = [dict(row) for row in rows]
    found = await message_diagnostics(pool, [row['id'] for row in values])
    return [row | {'review_diagnosis': found[row['id']]} for row in values]
