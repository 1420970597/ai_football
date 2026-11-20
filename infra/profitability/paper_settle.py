"""Recompute paper returns at the observed price using a versioned result ledger."""

import asyncio
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
import sys
from uuid import UUID

import asyncpg
from app.config import database_url
from app.settlement import settle


RESULTS = """
SELECT s.id,s.fixture_id, r.version,r.received_at,r.status AS result_status,l.created_at AS computed_at,
       l.calculation::text,l.outcome,l.unit_net,l.effective_stake
FROM v2_expert_signals s
LEFT JOIN LATERAL (
    SELECT f.version,f.received_at,f.status FROM v2_fixture_results f
    WHERE f.fixture_id=s.fixture_id AND f.received_at <= $2 ORDER BY f.version DESC LIMIT 1
) r ON true
LEFT JOIN LATERAL (
    SELECT calculation,outcome,unit_net,effective_stake,created_at FROM v2_signal_settlements l
    WHERE l.signal_id=s.id AND l.fixture_id=s.fixture_id AND l.result_version=r.version AND l.created_at <= $2
    ORDER BY created_at DESC,algorithm_version DESC LIMIT 1
) l ON true
WHERE s.id=ANY($1::uuid[])
"""
SLIPPAGE_PROFIT_HAIRCUT = Decimal('0.02')


def frames_digest(frames):
    return sha256(json.dumps(frames, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def settle_paper(offer, ledger):
    if not ledger or not ledger['calculation'] or ledger['result_status'] not in {'confirmed', 'void'}:
        return {'status': 'versioned_settlement_pending'}
    checked = datetime.fromisoformat(offer['native_quote_checked_at'])
    if ledger['received_at'] <= checked or ledger['computed_at'] < ledger['received_at']:
        return {'status': 'result_timing_unverified'}
    calculation = json.loads(ledger['calculation'])
    inputs, facts = calculation['signal'], calculation['result']
    identity = {'sport': 'sport', 'kind': 'market_kind', 'phase': 'phase', 'period': 'period',
                'selection': 'selection', 'score_basis': 'score_basis', 'score_baseline': 'score_baseline'}
    input_line = Decimal(inputs['line']) if inputs['line'] is not None else None
    offer_line = Decimal(offer['line']) if offer['line'] is not None else None
    if (ledger['fixture_id'] != offer['fixture_id'] or any(inputs[key] != offer[field] for key, field in identity.items())
            or input_line != offer_line or Decimal(inputs['price']) != Decimal(offer['recommendation_price'])
            or facts['status'] != ledger['result_status']):
        return {'status': 'result_contract_mismatch'}
    effective = calculation.get('effective_signal', inputs)
    if {k: v for k, v in effective.items() if k != 'score_basis'} != {
        k: v for k, v in inputs.items() if k != 'score_basis'
    }:
        return {'status': 'result_contract_mismatch'}
    original = settle(effective, facts)
    if (original.unit_net is None or original.outcome != ledger['outcome']
            or original.unit_net.quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP) != ledger['unit_net']
            or original.effective_stake != ledger['effective_stake']):
        return {'status': 'stored_settlement_not_reproduced'}
    native_price = Decimal(offer['native_selected_price'])
    stressed_price = 1 + (native_price - 1) * (1 - SLIPPAGE_PROFIT_HAIRCUT)
    paper = settle(effective | {'price': str(native_price)}, facts)
    stressed = settle(effective | {'price': str(stressed_price)}, facts)
    if paper.unit_net is None or stressed.unit_net is None or paper.outcome != original.outcome:
        return {'status': 'paper_settlement_unavailable'}
    return {'status': 'paper_quote_diagnostic', 'result_version': ledger['version'],
            'result_received_at': ledger['received_at'].isoformat(),
            'result_computed_at': ledger['computed_at'].isoformat(), 'outcome': paper.outcome,
            'quoted_unit_return': str(paper.unit_net), 'stressed_unit_return_before_unknown_costs': str(stressed.unit_net),
            'validation_eligible': False, 'missing_evidence': ['independent_timed_depth', 'account_cost_and_currency']}


async def read_results(offers, cutoff):
    if cutoff.utcoffset() != timedelta(0):
        raise ValueError('UTC cutoff required')
    ids = [UUID(offer['signal_id']) for offer in offers]
    if len(ids) != len(set(ids)):
        raise ValueError('paper signals must appear once')
    conn = await asyncpg.connect(database_url(), timeout=5, command_timeout=10,
                                 server_settings={'default_transaction_read_only': 'on', 'statement_timeout': '8000'})
    try:
        async with conn.transaction(isolation='repeatable_read', readonly=True):
            if cutoff > await conn.fetchval('SELECT transaction_timestamp()'):
                raise ValueError('cannot assess future outcomes')
            results = {}
            for start in range(0, len(ids), 512):
                rows = await conn.fetch(RESULTS, ids[start:start + 512], cutoff)
                results.update((str(row['id']), row) for row in rows)
            return results
    finally:
        await conn.close()


async def score(frames, cutoff):
    slots = set()
    statuses = Counter()
    offers = []
    for frame in frames:
        slot = frame['slot']
        if slot in slots or datetime.fromisoformat(frame['observed_at']) > cutoff:
            raise ValueError('duplicate or future paper slot')
        slots.add(slot)
        statuses.update(row['status'] for row in frame['rows'])
        offers.extend(row for row in frame['rows'] if row['status'] == 'native_preflight_available')
    if any(datetime.fromisoformat(row['native_quote_checked_at']) > cutoff for row in offers):
        raise ValueError('future paper preflight')
    ledger = await read_results(offers, cutoff) if offers else {}
    settlements = [
        {key: offer[key] for key in ('signal_id', 'fixture_id', 'sport', 'phase', 'market_kind',
                                     'native_quote_checked_at', 'native_selected_price')} |
        settle_paper(offer, ledger.get(offer['signal_id']))
        for offer in offers
    ]
    return {'cutoff': cutoff.isoformat(), 'slots': len(slots), 'frames_sha256': frames_digest(frames),
            'discovery_statuses': dict(statuses),
            'paper_settlements': settlements}


if __name__ == '__main__':
    try:
        print(json.dumps(asyncio.run(score(json.load(sys.stdin), datetime.fromisoformat(sys.argv[1]))),
                         sort_keys=True))
    except Exception as exc:
        raise SystemExit(f'paper settlement failed: {type(exc).__name__}') from None
