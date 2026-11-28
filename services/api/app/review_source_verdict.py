"""Evidence-bound operator maintenance; never an automatic image classifier."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

REASONS = frozenset({'source_period_not_supported', 'source_selection_not_supported'})


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    default=str).encode()).hexdigest()


@dataclass(frozen=True)
class SourceIntentVerdict:
    """A prior, explicit visual review anchored to one source and signal."""
    reason: str
    source_sha256: str
    supporting_evidence_sha256: tuple[str, ...]
    settled_orders_sha256: str | None = None

    def __post_init__(self):
        if (not isinstance(self.supporting_evidence_sha256, tuple)
                or not 1 <= len(self.supporting_evidence_sha256) <= 8):
            raise ValueError('source verdict requires bounded supporting evidence')
        hashes = (self.source_sha256, *self.supporting_evidence_sha256)
        if self.settled_orders_sha256 is not None:
            hashes += (self.settled_orders_sha256,)
        if (self.reason not in REASONS
                or any(not isinstance(v, str) or not re.fullmatch('[0-9a-f]{64}', v) for v in hashes)):
            raise ValueError('source verdict requires a bounded reason and evidence digests')


def source_fingerprint(row, signal):
    return _digest({'message': {key: row[key] for key in (
        'message_id', 'source_id', 'platform_message_id', 'revision', 'raw_text',
        'posted_at', 'edited_at', 'media_state', 'media_objects', 'reply_context')},
        'signal': {key: signal[key] for key in (
            'id', 'message_id', 'fixture_id', 'market_id', 'market_kind', 'period',
            'line', 'selection', 'recommendation_price', 'quote_time', 'evidence_text')}})


def source_verdict_problem(verdict, row, signal):
    images = row['media_objects']
    if (row['media_state'] != 'ready' or not images or any(
            not isinstance(x, dict) or not isinstance(x.get('sha256'), str)
            or not re.fullmatch('[0-9a-f]{64}', x['sha256'])
            or x.get('mime_type') not in {'image/jpeg', 'image/png', 'image/webp'} for x in images)):
        return 'visual_source_evidence_missing'
    if source_fingerprint(row, signal) != verdict.source_sha256:
        return 'source_or_signal_evidence_changed'
    return None


async def settled_order_evidence(conn, signal_id, *, lock=False):
    """Return hashes only. A correction never changes an order or its receipts."""
    rows = await conn.fetch(
        "SELECT o.*,r.receipt_sha256 AS correction_receipt_sha256,r.payload AS correction_receipt,"
        "EXISTS(SELECT 1 FROM v2_audit_log a WHERE a.resource_type='bet_order' "
        "AND a.resource_id=o.id::text AND a.action='order_reconciled' "
        "AND a.details->>'state'='settled') AS correction_settlement_audited "
        "FROM v2_bet_orders o LEFT JOIN LATERAL(SELECT receipt_sha256,payload "
        "FROM v2_order_receipt_evidence WHERE order_id=o.id AND payload->>'state'='settled' "
        "ORDER BY observed_at DESC,id DESC LIMIT 1)r ON TRUE "
        "WHERE o.signal_id=$1 ORDER BY o.id LIMIT 33" + (' FOR UPDATE OF o' if lock else ''), signal_id)
    valid = len(rows) <= 32
    for row in rows:
        receipt = row['correction_receipt']
        try:
            valid = valid and bool(
                row['state'] == 'settled' and row['reserved_stake'] == 0
                and row['settled_net'] is not None and row['settled_at'] is not None
                and row['receipt_observed_at'] is not None and row['provider_order_no']
                and row['correction_settlement_audited'] and isinstance(receipt, dict)
                and _digest(receipt) == row['correction_receipt_sha256']
                and receipt.get('order_no') == row['provider_order_no']
                and Decimal(receipt['net_profit']) == row['settled_net']
                and datetime.fromisoformat(receipt['settled_at'].replace('Z', '+00:00')) == row['settled_at'])
        except (KeyError, TypeError, ValueError, InvalidOperation):
            valid = False
    return {'count': len(rows), 'complete': len(rows) <= 32,
            'all_settled_with_verified_receipts': valid,
            'sha256': _digest([dict(row) for row in rows])}
