"""Append-only maintenance of completed receipts; never reruns a review."""
from .admin_reviews import context
from .auth import audit
from .review_state import trace
from .review_summary import make_summary, SUMMARY_ITEM_LIMIT
from .review_summary_evidence import read_summary_evidence

ALGORITHM = 'recovered-quote-receipt-v1'
PRECISION_ALGORITHM = 'receipt-jsonb-precision-v1'
_UNCHANGED = ('schema_version', 'basis', 'review_mode', 'accepted_count', 'items',
              'identified_without_signal', 'identified_omitted_count',
              'completion_verified', 'completion_issues', 'omitted_count')


async def correct_recovered_receipt(conn, job_id, *, apply=False):
    """Caller provides a transaction and explicit maintenance authorization."""
    if not conn.is_in_transaction():
        raise RuntimeError('receipt correction requires a transaction')
    row = await context(conn, job_id)
    result = {'job_id': str(job_id), 'algorithm': ALGORITHM, 'applied': False}
    if row is None:
        return {**result, 'status': 'ineligible', 'reason': 'job_missing'}
    if apply:
        # Use the same logical-message/job/message order as administrative retry
        # so edits, deletes and a concurrent correction cannot pass our checks.
        await conn.execute('SELECT pg_advisory_xact_lock(hashtext($1))',
                           f"telegram:{row['source_id']}:{row['platform_message_id']}")
        await conn.fetchrow('SELECT id FROM v2_analysis_jobs WHERE id=$1 FOR UPDATE', job_id)
        await conn.fetchrow('SELECT id FROM v2_expert_messages WHERE id=$1 FOR SHARE', row['message_id'])
        row = await context(conn, job_id)
    latest_job = await conn.fetchval(
        'SELECT id FROM v2_analysis_jobs WHERE message_id=$1 ORDER BY created_at DESC,id DESC LIMIT 1',
        row['message_id'])
    if (row['state'] not in {'succeeded', 'unresolved'} or row['has_active_job']
            or row['deleted_at'] or not row['is_latest'] or latest_job != job_id
            or row['has_event'] and not row['event_current']):
        return {**result, 'status': 'ineligible', 'reason': 'not_current_completed_review'}
    original = await conn.fetchrow(
        "SELECT id,payload FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='system_review_result' "
        'ORDER BY sequence_no DESC,id DESC LIMIT 1', job_id)
    if not original or original['payload'].get('attempt_id') != str(row['attempt_id']):
        return {**result, 'status': 'ineligible', 'reason': 'current_attempt_receipt_missing'}
    payload = original['payload']
    if payload.get('completion_verified') is not True:
        return {**result, 'status': 'ineligible', 'reason': 'completion_not_verified'}
    before = payload.get('tool_rejection_counts')
    if not isinstance(before, dict) or any(type(n) is not int or n < 1 for n in before.values()):
        return {**result, 'status': 'ineligible', 'reason': 'invalid_rejection_counts'}
    precision_base = None
    if not before and payload.get('receipt_correction', {}).get('algorithm') == ALGORITHM:
        # Compare and preserve PostgreSQL numerics before a Python JSON decoder
        # can round their original representation through a binary float.
        precision_base = await conn.fetchval("""
            SELECT o.id FROM v2_analysis_traces o JOIN v2_analysis_traces t ON t.id=$1
            WHERE o.id::text=t.payload->'receipt_correction'->>'previous_trace_id'
            AND o.job_id=t.job_id AND o.trace_kind='system_review_result'
            AND o.payload->'attempt_id'=t.payload->'attempt_id'
            AND NOT o.payload ? 'receipt_correction'
            AND (o.payload - ARRAY['tool_rejection_counts','content','claim_to_conclusion_ms'])
              = (t.payload - ARRAY['tool_rejection_counts','content','claim_to_conclusion_ms','receipt_correction'])
            AND o.payload->'claim_to_conclusion_ms' IS DISTINCT FROM t.payload->'claim_to_conclusion_ms'
            AND jsonb_typeof(o.payload->'claim_to_conclusion_ms')='number'
            AND jsonb_typeof(t.payload->'claim_to_conclusion_ms')='number'
            AND abs((o.payload->>'claim_to_conclusion_ms')::numeric
                    -(t.payload->>'claim_to_conclusion_ms')::numeric)<0.000001
            """, original['id'])
    if not before and precision_base is None:
        return {**result, 'status': 'not_needed'}
    evidence = await read_summary_evidence(conn, job_id, row['attempt_id'], row['message_id'], SUMMARY_ITEM_LIMIT)
    receipt = make_summary(evidence.signals, accepted_total=evidence.total, rejected=evidence.rejected,
                           review_mode=payload['review_mode'], identified=evidence.identified,
                           identified_total=evidence.identified[0]['total'] if evidence.identified else 0)
    if any(payload.get(key) != receipt.get(key) for key in _UNCHANGED):
        return {**result, 'status': 'ineligible', 'reason': 'accepted_evidence_or_scope_changed'}
    after = receipt['tool_rejection_counts']
    if any(count > before.get(reason, 0) for reason, count in after.items()):
        return {**result, 'status': 'ineligible', 'reason': 'rejection_scope_changed'}
    removed = sum(before.values()) - sum(after.values())
    if removed <= 0 and precision_base is None:
        return {**result, 'status': 'not_needed'}
    correction = {'algorithm': ALGORITHM, 'previous_trace_id': str(original['id']),
                  'reason': 'same_quote_later_accepted_with_immutable_evidence',
                  'removed_rejections': removed}
    if precision_base is not None:
        correction = {'algorithm': PRECISION_ALGORITHM, 'previous_trace_id': str(original['id']),
                      'original_trace_id': str(precision_base),
                      'reason': 'preserve_original_database_numeric_without_float_roundtrip'}
        result['algorithm'] = PRECISION_ALGORITHM
    result |= {'status': 'would_correct', 'before_rejections': before, 'after_rejections': after,
               'previous_trace_id': str(original['id']), 'removed_rejections': removed}
    if not apply:
        return result
    changes = {**receipt, 'receipt_correction': correction,
               'content': receipt['content'] + ('\n精度更正：沿用原始数据库耗时数值；原始审核记录保留。'
                   if precision_base else '\n汇总更正：已移除后来成功录入的拒绝项；原始审核记录保留。')}
    await trace(conn, job_id, 'system_review_result', changes)
    # trace holds the job's transaction-scoped sequence lock. Complete the new,
    # still invisible row from the original JSONB, without decoding its numbers.
    # A failure rolls back both statements; existing traces are never modified.
    updated = await conn.fetchval("""
        UPDATE v2_analysis_traces t SET payload=o.payload || t.payload
        FROM v2_analysis_traces o WHERE o.id=$2 AND t.id=(
            SELECT id FROM v2_analysis_traces WHERE job_id=$1
            ORDER BY sequence_no DESC,id DESC LIMIT 1)
        AND t.trace_kind='system_review_result' RETURNING t.id
        """, job_id, precision_base or original['id'])
    if updated is None:
        raise RuntimeError('new receipt could not preserve original JSONB')
    await audit(conn, None, 'system', 'review_receipt_corrected', 'analysis_job', str(job_id),
                details=correction)
    return {**result, 'status': 'corrected', 'applied': True}
