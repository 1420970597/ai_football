"""Explicit maintenance of source-intent-invalid signals, with append-only evidence."""
from .admin_reviews import context
from .auth import audit
from .review_period_intent import CORRECTION_REASONS, period_intent_rejection
from .review_line_intent import line_intent_rejection
from .review_state import trace
from .review_summary import record_summary
from .review_source_verdict import SourceIntentVerdict, source_verdict_problem, settled_order_evidence

ALGORITHM = 'source-period-intent-correction-v1'
LINE_ALGORITHM = 'source-line-intent-correction-v1'
SOURCE_ALGORITHM = 'verified-visual-source-intent-correction-v1'


async def correct_period_signal(conn, job_id, signal_id, *, apply=False):
    return await _correct_signal(conn, job_id, signal_id, apply=apply)


async def correct_line_signal(conn, job_id, signal_id, *, apply=False):
    return await _correct_signal(conn, job_id, signal_id, apply=apply, source_line=True)


async def correct_verified_source_signal(conn, job_id, signal_id, verdict, *, apply=False):
    """Explicit maintenance authority is required; this is not a model tool."""
    if not isinstance(verdict, SourceIntentVerdict):
        raise ValueError('an evidence-bound source verdict is required')
    return await _correct_signal(conn, job_id, signal_id, apply=apply, source_verdict=verdict)


async def _correct_signal(conn, job_id, signal_id, *, apply, source_line=False, source_verdict=None):
    """Caller owns transaction and explicit administrative maintenance authority."""
    if not conn.is_in_transaction():
        raise RuntimeError('signal correction requires a transaction')
    result = {'job_id': str(job_id), 'signal_id': str(signal_id),
              'algorithm': SOURCE_ALGORITHM if source_verdict else LINE_ALGORITHM if source_line else ALGORITHM, 'applied': False}
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
    if source_verdict:
        problem = source_verdict_problem(source_verdict, row, signal)
        if problem:
            return result | {'status': 'ineligible', 'reason': problem}
    if signal['withdrawn_at']:
        if source_verdict and signal['withdrawal_reason'] != 'review_' + source_verdict.reason:
            return result | {'status': 'ineligible', 'reason': 'signal_already_withdrawn_for_other_reason'}
        return result | {'status': 'not_needed' if signal['withdrawal_reason'] in CORRECTION_REASONS
                         else 'ineligible', 'reason': 'signal_already_withdrawn'}
    source_proof = {}
    if source_verdict:
        orders = await settled_order_evidence(conn, signal_id, lock=apply)
        if orders['count'] and (not orders['all_settled_with_verified_receipts']
                or source_verdict.settled_orders_sha256 != orders['sha256']):
            return result | {'status': 'ineligible', 'reason': 'signal_has_order_requires_separate_reconciliation'}
        if not orders['count'] and source_verdict.settled_orders_sha256 is not None:
            return result | {'status': 'ineligible', 'reason': 'order_evidence_changed'}
        source_proof = {'source_sha256': source_verdict.source_sha256,
                        'supporting_evidence_sha256': list(source_verdict.supporting_evidence_sha256),
                        'settled_orders': orders, 'order_financial_records_preserved': True,
                        'explicit_operator_verdict': True}
    elif await conn.fetchval('SELECT EXISTS(SELECT 1 FROM v2_bet_orders WHERE signal_id=$1)', signal_id):
        return result | {'status': 'ineligible', 'reason': 'signal_has_order_requires_separate_reconciliation'}
    evidence = await conn.fetchrow(
        "SELECT evidence FROM v2_quote_evidence WHERE evidence_kind='expert_signal' "
        'AND reference_id=$1 ORDER BY captured_at DESC LIMIT 1', signal_id)
    period_context = evidence['evidence'].get('source_period_context') if evidence else None
    if (not source_line and not source_verdict and isinstance(period_context, dict)
            and period_context.get('algorithm') in {'leyu-early-first-half-stage-v1', 'leyu-early-first-half-stage-v2'}
            and (period_context['algorithm'] == 'leyu-early-first-half-stage-v1'
                 or period_context.get('source_age_limit_seconds') == 300)
            and period_context.get('fixture_id') == signal['fixture_id']
            and period_context.get('source_id') == signal['fixture_id']
            and period_context.get('match_state') == '6'
            and all(type(period_context.get(key)) is int and 0 <= period_context[key] < 45
                    for key in ('search_clock_minute', 'quote_clock_minute'))):
        return result | {'status': 'not_needed', 'reason': 'period_context_verified_at_acceptance'}
    reason = (source_verdict.reason if source_verdict else line_intent_rejection(signal['evidence_text'], signal) if source_line
              else period_intent_rejection(row['raw_text'], signal))
    if reason is None:
        return result | {'status': 'not_needed', 'reason': 'no_verified_line_conflict' if source_line
                         else 'no_verified_period_conflict'}
    original = await conn.fetchrow(
        "SELECT id,payload FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='system_review_result' "
        'ORDER BY sequence_no DESC,id DESC LIMIT 1', job_id)
    if not original or original['payload'].get('attempt_id') != str(row['attempt_id']):
        return result | {'status': 'ineligible', 'reason': 'current_attempt_receipt_missing'}
    correction = {'algorithm': result['algorithm'], 'reason': reason, 'signal_id': str(signal_id),
                  'previous_trace_id': str(original['id']), 'previous_job_state': row['state'],
                  'previous_outcome': signal['outcome'], 'original_quote_preserved': True, **source_proof}
    result |= {'status': 'would_correct', 'reason': reason}
    if not apply:
        return result
    await conn.execute(
        'UPDATE v2_expert_signals SET withdrawn_at=now(),withdrawal_reason=$2,'
        "outcome='pending',unit_net=NULL,effective_stake=0,settled_at=NULL,"
        "settlement_reason=$3 WHERE id=$1",
        signal_id, 'review_' + reason, 'recommendation_source_intent_invalid' if source_verdict else 'recommendation_source_line_intent_invalid' if source_line
        else 'recommendation_source_period_intent_invalid')
    await trace(conn, job_id, 'system_signal_correction', {'attempt_id': str(row['attempt_id']), **correction})
    remaining = await record_summary(conn, {'id': job_id, 'message_id': row['message_id']},
                                     row['attempt_id'], row['review_mode'])
    # Preserve original timing as database JSON numerics, without float reencoding.
    await conn.execute(
        "UPDATE v2_analysis_traces t SET payload=t.payload || jsonb_build_object("
        "'signal_correction',$3::jsonb,'outcome',$4::text,'content',t.payload->>'content' || $5::text) "
        "|| (o.payload-ARRAY['schema_version','language','basis','review_mode','accepted_count','items',"
        "'identified_without_signal','identified_omitted_count','completion_verified','completion_issues',"
        "'omitted_count','tool_rejection_counts','content','outcome','receipt_correction','signal_correction']) "
        'FROM v2_analysis_traces o WHERE o.id=$2 AND t.id=('
        'SELECT id FROM v2_analysis_traces WHERE job_id=$1 ORDER BY sequence_no DESC,id DESC LIMIT 1)',
        job_id, original['id'], correction, 'succeeded' if remaining else 'unresolved',
        '\n纠错：该录入缺少来源选择或期间证据，已撤销并排除专家统计；原文、报价、历史审核及真实订单财务记录保留。' if source_verdict else '\n纠错：原推荐盘口线与来源证据不一致，该条录入已撤销并排除统计；原始报价及审核记录保留。' if source_line
        else '\n纠错：原推荐期间无法核验，该条录入已撤销并排除统计；原始报价及审核记录保留。')
    await conn.execute('UPDATE v2_analysis_jobs SET state=$2,updated_at=now() WHERE id=$1',
                       job_id, 'succeeded' if remaining else 'unresolved')
    await audit(conn, None, 'system', 'review_signal_source_intent_corrected' if source_verdict else 'review_signal_line_corrected' if source_line
                else 'review_signal_period_corrected', 'expert_signal',
                str(signal_id), details=correction)
    return result | {'status': 'corrected', 'applied': True}
