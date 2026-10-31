"""订单锁内追加去秘密的预检/发送许可证据；许可不是成交回执。"""
from __future__ import annotations

from datetime import timezone
from decimal import Decimal
import hashlib
import json
import uuid

from fastapi import HTTPException

STAGES = frozenset({'preflight_validated', 'submission_authorized'})
NAMESPACE = uuid.UUID('c8b0c1d5-5c51-4b61-a35c-1611ba2819dc')


def _decimal(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError('nonfinite order evidence number')
    return format((number if number else Decimal(0)).normalize(), 'f')


def stage_document(order, signal, attempt_id, prepared_digest, stage):
    """Construct from a fixed allowlist, never from a caller's payload document."""
    if stage not in STAGES or order['attempt_id'] != attempt_id:
        raise HTTPException(409, '订单证据阶段或尝试不匹配')
    source_time = signal['source_updated_at']
    if source_time is None or source_time.tzinfo is None or source_time.utcoffset() is None:
        raise HTTPException(409, '订单证据缺少有效报价源时间')
    document = {
        'contract': 'order-stage-v1', 'stage': stage, 'submission_confirmed': False,
        'order_id': str(order['id']), 'attempt_id': str(attempt_id),
        'user_id': str(order['user_id']), 'account_id': str(order['account_id']),
        'signal_id': str(order['signal_id']), 'quote_id': str(signal['quote_id']),
        'binding_revision': order['binding_revision'], 'config_revision': order['config_revision'],
        'policy_revision': order['policy_revision'], 'prepared_digest': prepared_digest,
        'fixture_id': signal['fixture_id'], 'market_id': signal['market_id'],
        'provider_match_id': order['quote_snapshot']['match_id'],
        'market_kind': signal['current_kind'], 'period': signal['period'],
        'line': None if signal['line'] is None else _decimal(signal['line']),
        'selection': signal['selection'], 'price': _decimal(signal['current_price']),
        'stake': _decimal(order['stake']), 'currency': 'CNY',
        'odds_change_policy': 'reject', 'order_type': 'single',
        'source_updated_at': source_time.astimezone(timezone.utc).isoformat(),
        'play_id': signal['play_id'], 'market_instance_id': signal['market_instance_id'],
        'selection_id': signal['selection_id'], 'score_basis': signal['current_basis'],
        'phase': signal['current_phase'],
        'score_benchmark': order['quote_snapshot']['score_benchmark'],
        'score_baseline': {'home': signal['home_score'], 'away': signal['away_score']},
    }
    if stage == 'submission_authorized':
        document['preflight_evidence_id'] = str(stage_identity(order['id'], attempt_id, 'preflight_validated'))
    encoded = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return document | {'content_sha256': hashlib.sha256(encoded.encode()).hexdigest()}


def stage_identity(order_id, attempt_id, stage):
    return uuid.uuid5(NAMESPACE, f'{order_id}:{attempt_id}:{stage}')


async def append_stage(conn, order, signal, attempt_id, prepared_digest, stage):
    """Caller must hold the order lock and transaction; conflicting evidence fails closed."""
    if not conn.is_in_transaction():
        raise RuntimeError('order stage evidence requires the authorization transaction')
    document = stage_document(order, signal, attempt_id, prepared_digest, stage)
    identity = stage_identity(order['id'], attempt_id, stage)
    values = (
        identity, signal['fixture_id'], signal['market_id'], signal['market_name'], signal['period'],
        signal['line'], signal['selection'], signal['current_price'], order['id'],
        signal['source_updated_at'], document, document['score_baseline'],
    )
    inserted = await conn.fetchval(
        "INSERT INTO v2_quote_evidence(id,fixture_id,market_id,market_name,period,line,selection,price,"
        "evidence_kind,reference_id,source_updated_at,evidence,score_baseline) "
        "VALUES($1,$2,$3,$4,$5,$6,$7,$8,'order_attempt',$9,$10,$11,$12) "
        "ON CONFLICT(id) DO NOTHING RETURNING id", *values,
    )
    if not inserted:
        existing = await conn.fetchrow(
            'SELECT id,fixture_id,market_id,market_name,period,line,selection,price,reference_id,'
            "source_updated_at,evidence,score_baseline FROM v2_quote_evidence WHERE id=$1 AND evidence_kind='order_attempt'",
            identity,
        )
        if existing is None or tuple(existing.values()) != values:
            raise HTTPException(409, '订单阶段证据冲突，保留首次不可变记录')
    return identity
