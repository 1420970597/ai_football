"""Explicit maintenance of period-invalid signals, with append-only evidence."""
from .admin_reviews import context
from .auth import audit
from .review_period_intent import CORRECTION_REASONS, period_intent_rejection
from .review_state import trace
from .review_summary import record_summary

ALGORITHM = 'source-period-intent-correction-v1'


async def correct_period_signal(conn, job_id, signal_id, *, apply=False):
    """Caller owns transaction and explicit administrative maintenance authority."""
    if not conn.is_in_transaction():
        raise RuntimeError('signal correction requires a transaction')
    result = {'job_id': str(job_id), 'signal_id': str(signal_id),
              'algorithm': ALGORITHM, 'applied': False}
    row = await context(conn, job_id)
    signal = await conn.fetchrow('SELECT * FROM v2_expert_signals WHERE id=$1', signal_id)
    if not row or not signal or signal['message_id'] != row['message_id']:
        return result | {'status': 'ineligible', 'reason': 'signal_job_mismatch'}
    if apply:
        await conn.execute('SELECT pg_advisory_xact_lock(hashtext($1))',
                           f"telegram:{row['source_id']}:{row['platform_message_id']}")
        # Match result ingestion's fixture-before-signal lock order.
        await conn.fetchrow('SELECT id FROM v2_fixtures WHERE id=$1 FOR NO KEY UPDATE', signal['fixture_id'])
        await conn.fetchrow('SELECT id FROM v2_analysis_jobs WHERE id=$1 FOR UPDATE', job_id)
        await conn.fetchrow('SELECT id FROM v2_expert_messages WHERE id=$1 FOR SHARE', row['message_id'])
        row = await context(conn, job_id)
        signal = await conn.fetchrow('SELECT * FROM v2_expert_signals WHERE id=$1 FOR UPDATE', signal_id)
    latest = await conn.fetchval('SELECT id FROM v2_analysis_jobs WHERE message_id=$1 '
                                'ORDER BY created_at DESC,id DESC LIMIT 1', row['message_id'])
    if (row['state'] not in {'succeeded', 'unresolved'} or row['has_active_job']
            or row['deleted_at'] or not row['is_latest'] or latest != job_id
            or row['has_event'] and not row['event_current']):
        return result | {'status': 'ineligible', 'reason': 'not_current_completed_review'}
    if signal['withdrawn_at']:
        return result | {'status': 'not_needed' if signal['withdrawal_reason'] in CORRECTION_REASONS
                         else 'ineligible', 'reason': 'signal_already_withdrawn'}
    if await conn.fetchval('SELECT EXISTS(SELECT 1 FROM v2_bet_orders WHERE signal_id=$1)', signal_id):
        return result | {'status': 'ineligible', 'reason': 'signal_has_order_requires_separate_reconciliation'}
    reason = period_intent_rejection(row['raw_text'], signal)
    if reason is None:
        return result | {'status': 'not_needed', 'reason': 'no_verified_period_conflict'}
    original = await conn.fetchrow(
        "SELECT id,payload FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='system_review_result' "
        'ORDER BY sequence_no DESC,id DESC LIMIT 1', job_id)
    if not original or original['payload'].get('attempt_id') != str(row['attempt_id']):
        return result | {'status': 'ineligible', 'reason': 'current_attempt_receipt_missing'}
    correction = {'algorithm': ALGORITHM, 'reason': reason, 'signal_id': str(signal_id),
                  'previous_trace_id': str(original['id']), 'previous_job_state': row['state'],
                  'previous_outcome': signal['outcome'], 'original_quote_preserved': True}
    result |= {'status': 'would_correct', 'reason': reason}
    if not apply:
        return result
    await conn.execute(
        'UPDATE v2_expert_signals SET withdrawn_at=now(),withdrawal_reason=$2,'
        "outcome='pending',unit_net=NULL,effective_stake=0,settled_at=NULL,"
        "settlement_reason='recommendation_source_period_intent_invalid' WHERE id=$1",
        signal_id, 'review_' + reason)
    await trace(conn, job_id, 'system_signal_correction', {'attempt_id': str(row['attempt_id']), **correction})
    remaining = await record_summary(conn, {'id': job_id, 'message_id': row['message_id']},
                                     row['attempt_id'], row['review_mode'])
    # Preserve original timing as database JSON numerics, without float reencoding.
    await conn.execute(
        "UPDATE v2_analysis_traces t SET payload=t.payload || jsonb_build_object("
        "'signal_correction',$3::jsonb,'outcome',$4::text,'content',t.payload->>'content' || '\n纠错：原推荐期间无法核验，该条录入已撤销并排除统计；原始报价及审核记录保留。') "
        "|| (o.payload-ARRAY['schema_version','language','basis','review_mode','accepted_count','items',"
        "'identified_without_signal','identified_omitted_count','completion_verified','completion_issues',"
        "'omitted_count','tool_rejection_counts','content','outcome','receipt_correction','signal_correction']) "
        'FROM v2_analysis_traces o WHERE o.id=$2 AND t.id=('
        'SELECT id FROM v2_analysis_traces WHERE job_id=$1 ORDER BY sequence_no DESC,id DESC LIMIT 1)',
        job_id, original['id'], correction, 'succeeded' if remaining else 'unresolved')
    await conn.execute('UPDATE v2_analysis_jobs SET state=$2,updated_at=now() WHERE id=$1',
                       job_id, 'succeeded' if remaining else 'unresolved')
    await audit(conn, None, 'system', 'review_signal_period_corrected', 'expert_signal',
                str(signal_id), details=correction)
    return result | {'status': 'corrected', 'applied': True}
