"""P05: recommendation, preflight and fill price bridge.

Only an **explicitly archived fill price** can become the third stage. The
study never solves a fill price from a net amount: without a recorded
``fill_price`` (or ``execution_price`` on the receipt) the result is a partial
two-stage bridge, not a reconstruction.

When a fill price, a verified win/loss share, a stake, fees and a currency are
all present, the receipt is recomputed from the share and the fill price and
the residual against the archived net is reported. Unknown fees, unknown
currency, a zero-payout refund and a contract mismatch each keep their own
status; none of them is silently replaced by zero.
"""

from collections import Counter, defaultdict
from decimal import Decimal
import re

from infra.profitability.experiments import inputs
from infra.profitability.experiments.stats import counter, delays, stratified, unique

STUDY = 'P05'
THRESHOLD_SECONDS = Decimal(30)
SHARES = {
    'win': Decimal(1),
    'half_win': Decimal('0.5'),
    'push': Decimal(0),
    'void': Decimal(0),
    'half_loss': Decimal('-0.5'),
    'loss': Decimal(-1),
}
CONTRACT_FIELDS = ('fixture_id', 'market_kind', 'selection', 'period', 'line')


def _fill_price(row, receipt):
    for value in (row.get('fill_price'), row.get('execution_price'),
                  receipt.get('fill_price'), receipt.get('execution_price')):
        if value is not None:
            return inputs.number(value), 'archived'
    return None, None


def _share(receipt):
    for key in ('outcome_share', 'win_share'):
        if receipt.get(key) is not None:
            return inputs.number(receipt[key])
    return SHARES.get(receipt.get('outcome'))


def _contract_mismatch(row, receipt):
    contract = receipt.get('contract') if isinstance(receipt.get('contract'), dict) else receipt
    for field in CONTRACT_FIELDS:
        if field in contract and contract[field] != row.get(field):
            return field
    return None


def _receipt(row, receipt):
    """Return one recomputed receipt or a reason it cannot be recomputed."""
    fill, source = _fill_price(row, receipt)
    if fill is None:
        return {'status': 'missing_fill'}
    if fill <= 1:
        return {'status': 'invalid_fill_price'}
    mismatch = _contract_mismatch(row, receipt)
    if mismatch:
        return {'status': 'mismatched_contract', 'field': mismatch}
    share = _share(receipt)
    if share not in SHARES.values():
        return {'status': 'missing_outcome_share'}
    stake = inputs.number(receipt.get('stake'))
    net = inputs.number(receipt.get('net'))
    fees = inputs.number(receipt.get('fees'))
    if stake is None or stake <= 0 or net is None:
        return {'status': 'invalid_receipt_amount'}
    if fees is None or fees < 0:
        return {'status': 'missing_fees'}
    if not re.fullmatch(r'[A-Z]{3}', str(receipt.get('currency', ''))):
        return {'status': 'missing_currency'}
    gross = stake * share * (fill - 1) if share >= 0 else stake * share
    expected = gross - fees
    payout = inputs.number(receipt.get('payout'))
    flags = []
    if payout is not None:
        if payout == 0 and share > 0:
            flags.append('zero_payout_contradicts_win')
        if payout == 0 and share == 0:
            flags.append('zero_refund')
        if payout > 0 and share < 0:
            flags.append('positive_payout_contradicts_loss')
    return {
        'status': 'recomputed',
        'fill_price': fill,
        'fill_source': source,
        'share': share,
        'stake': stake,
        'fees': fees,
        'currency': receipt['currency'],
        'expected_net': expected,
        'receipt_net': net,
        'residual': net - expected,
        'flow_flags': flags,
    }


def analyze(native_document, frames=None):
    begin, end, cutoff = inputs.interval(native_document)
    rows, duplicate_rows = unique(inputs.rows(native_document), 'order_id')
    classes = Counter()
    problems = Counter()
    segment = defaultdict(list)
    residuals, strata_rows = [], []
    for row in rows:
        try:
            signal = inputs.utc(row['signal_at'])
            preflight = inputs.utc(row['preflight_at'])
            placed = inputs.utc(row['placed_at'])
        except (KeyError, TypeError, ValueError):
            problems['missing_timepoint'] += 1
            classes['invalid_order_clock'] += 1
            continue
        if not signal <= preflight <= placed:
            problems['time_order_violation'] += 1
            classes['invalid_order_clock'] += 1
            continue
        if placed >= cutoff:
            problems['entry_at_or_after_cutoff'] += 1
            classes['invalid_order_clock'] += 1
            continue
        recommendation = inputs.number(row.get('signal_price'))
        preflight_price = inputs.number(row.get('preflight_price'))
        if recommendation is None or recommendation <= 1 or preflight_price is None or preflight_price <= 1:
            classes['invalid_visible_price'] += 1
            continue
        visible = preflight_price - recommendation
        segment['recommendation_to_preflight'].append(visible)
        receipts = row.get('receipts') or []
        if not receipts:
            classes['missing_receipt'] += 1
            continue
        order_recomputed = False
        for receipt in receipts:
            result = _receipt(row, receipt)
            status = result['status']
            classes[status] += 1
            if status != 'recomputed':
                continue
            order_recomputed = True
            segment['preflight_to_fill'].append(result['fill_price'] - preflight_price)
            segment['recommendation_to_fill'].append(result['fill_price'] - recommendation)
            winning_principal = result['stake'] * max(result['share'], Decimal(0))
            segment['recommendation_to_preflight_payout_delta'].append(visible * winning_principal)
            segment['preflight_to_fill_payout_delta'].append(
                (result['fill_price'] - preflight_price) * winning_principal
            )
            residuals.append(result)
            for flag in result['flow_flags']:
                classes[flag] += 1
        if not order_recomputed and all(_fill_price(row, receipt)[0] is None for receipt in receipts):
            classes['partial_bridge_only'] += 1
        strata_rows.append({
            'sport': row.get('sport'),
            'market_kind': row.get('market_kind'),
            'recommendation_to_preflight': visible,
        })

    def measure(bucket):
        return {
            'orders': len(bucket),
            'recommendation_to_preflight': delays(
                [item['recommendation_to_preflight'] for item in bucket], THRESHOLD_SECONDS),
        }

    visible = None
    if frames:
        deltas = []
        for frame in frames:
            clock = inputs.frame_clock(frame)
            if 'slot' not in clock or clock['problems']:
                continue
            for row in frame.get('rows') or []:
                if row.get('status') != 'native_preflight_available':
                    continue
                recommendation = inputs.number(row.get('recommendation_price'))
                checked = inputs.number(row.get('native_selected_price'))
                if recommendation and checked and recommendation > 1 and checked > 1:
                    deltas.append(checked - recommendation)
        visible = {
            'stage': 'recommendation_to_preflight',
            'rows': len(deltas),
            'delta': delays(deltas, THRESHOLD_SECONDS),
            'fill': 'not archived',
        }

    residual_values = [item['residual'] for item in residuals]
    return {
        'experiment': STUDY,
        'interval': {'begin': begin, 'end': end, 'cutoff': cutoff},
        'denominator': {
            'orders': len(rows),
            'unique_orders': len(rows),
            'duplicate_orders': duplicate_rows,
            'fill_recomputed': len(residuals),
        },
        'problems': dict(sorted(problems.items())),
        'classes': counter(classes.elements()),
        'segments': {
            name: delays(values, THRESHOLD_SECONDS)
            for name, values in sorted(segment.items())
        },
        'residual': {
            'status': 'measured' if residual_values else 'insufficient_evidence',
            'rows': len(residual_values),
            'value': delays(residual_values, THRESHOLD_SECONDS),
            'currencies': dict(sorted(Counter(item['currency'] for item in residuals).items())),
        },
        'paper_visible': visible,
        'strata': stratified(strata_rows, ('sport', 'market_kind'), measure),
        'note': 'a missing fill price stays missing; it is never derived from the net receipt',
    }
