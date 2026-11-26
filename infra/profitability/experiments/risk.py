"""C04/C05: accepted-order capacity stress and same-fixture exposure replay.

Both studies read the frozen native cohort only. A receipt contributes money or
a return only after ``simulate_strategies.Receipt.parse`` accepts it and it is
settled and visible; a receipt that fails that contract is never repaired with
zero. Entry principal is taken from the source row when it exists. It is never
inferred from a later receipt, so an order without a source principal stays
explicitly unknown.

C04 reports a capped-stake/fee matrix and a profit-part slippage matrix. The
matrices are hypothetical stresses on accepted orders, not measured account
costs, and the unaccepted opportunity set is not estimable from this export.

C05 replays orders in native ``placed_at`` order. A same-fixture position is
released only by the first valid settled receipt that is strictly visible before
the next entry. A receipt observed at the same instant keeps the position
occupied. Final profit uses the latest valid settled receipt visible at the
cutoff, so a correction changes the outcome without rewriting the historical
decision. Conflicting versions with the same latest observation time are
ambiguous and excluded from outcome totals.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from infra.profitability.simulate_strategies import Receipt

ZERO = Decimal(0)
ONE = Decimal(1)
STAKE_LIMITS = (Decimal('5'), Decimal('10'), Decimal('50'), Decimal('100'))
FEE_RATES = (ZERO, Decimal('0.005'), Decimal('0.01'))
PROFIT_SLIPPAGE = (ZERO, Decimal('0.01'), Decimal('0.02'), Decimal('0.05'))
POLICIES = {'all': None, 'one_per_fixture': 1, 'two_per_fixture': 2}


def _moment(value):
    if hasattr(value, 'utcoffset'):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if moment.utcoffset() != timedelta(0):
        raise ValueError('expected UTC timestamp')
    return moment


def _source_principal(row):
    """Entry principal from the source row only; ``None`` means unknown."""
    for name in ('stake', 'principal', 'source'):
        value = row.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        try:
            amount = Decimal(str(value))
        except ArithmeticError:
            return None
        return amount if amount.is_finite() and amount > 0 else None
    return None


def _receipts(row):
    placed = _moment(row['placed_at'])
    values = []
    for raw in row.get('receipts') or ():
        receipt = Receipt.parse(raw, placed)
        values.append((raw, receipt))
    return placed, values


def _valid(values, cutoff):
    usable = [(raw, receipt) for raw, receipt in values
              if receipt.settled_at is not None and receipt.unit_return is not None
              and receipt.observed_at <= cutoff]
    return sorted(usable, key=lambda item: item[1].observed_at)


def _latest_valid(values, cutoff):
    usable = _valid(values, cutoff)
    if not usable:
        return None
    latest_at = usable[-1][1].observed_at
    latest = [item for item in usable if item[1].observed_at == latest_at]
    reference = latest[0][1]
    if any(receipt != reference for _, receipt in latest[1:]):
        return None
    return latest[-1]


def _first_settled(values, cutoff):
    for _, receipt in _valid(values, cutoff):
        if receipt.settled_at is not None:
            return receipt
    return None


def _document_rows(document):
    if not isinstance(document, dict) or not isinstance(document.get('rows'), list):
        raise ValueError('archive document requires a rows array')
    return document['rows']


def capacity(document):
    """C04: time-effective amounts and fixed stake/fee/slippage stresses."""
    rows = _document_rows(document)
    cutoff = _moment(document['cutoff'])
    observed, missing, valid_orders = 0, 0, []
    known_fee_orders = 0
    unknown_fee_orders = 0
    unknown_currency = 0
    for row in rows:
        try:
            _, values = _receipts(row)
        except (KeyError, TypeError, ValueError):
            missing += 1
            continue
        chosen = _latest_valid(values, cutoff)
        if chosen is None:
            missing += 1
            continue
        raw, receipt = chosen
        amount = Decimal(str(raw['stake']))
        if not amount.is_finite() or amount <= 0:
            missing += 1
            continue
        observed += 1
        if receipt.unit_fees is None:
            unknown_fee_orders += 1
        else:
            known_fee_orders += 1
        if receipt.currency is None:
            unknown_currency += 1
        valid_orders.append({'stake': amount, 'return': receipt.unit_return,
                             'fees': receipt.unit_fees})

    turnover = sum((item['stake'] for item in valid_orders), ZERO)
    units = sum((item['return'] for item in valid_orders), ZERO)
    known_fees = sum((item['fees'] * item['stake'] for item in valid_orders
                      if item['fees'] is not None), ZERO)

    stake_matrix = []
    for limit in STAKE_LIMITS:
        effective = sum((min(item['stake'], limit) for item in valid_orders), ZERO)
        contribution = sum((item['return'] * min(item['stake'], limit)
                            for item in valid_orders), ZERO)
        stake_matrix.append({'stake_limit': limit, 'orders': len(valid_orders),
                             'effective_turnover': effective,
                             'gross_units_at_limit': contribution})

    fee_matrix = []
    for rate in FEE_RATES:
        contribution = sum(((item['return'] - rate) * item['stake']
                            for item in valid_orders), ZERO)
        fee_matrix.append({'fee_rate_on_stake': rate, 'turnover': turnover,
                           'assumed_fee_amount': rate * turnover,
                           'units_after_assumed_fee': contribution})

    slippage_matrix = []
    for slippage in PROFIT_SLIPPAGE:
        contribution = sum((item['return'] - max(item['return'], ZERO) * slippage) * item['stake']
                           for item in valid_orders)
        slippage_matrix.append({'profit_part_slippage': slippage,
                                'turnover': turnover,
                                'units_after_slippage': contribution})

    return {
        'study': 'C04',
        'denominator': {'rows': len(rows), 'valid_receipt_orders': observed,
                        'orders_without_valid_receipt': missing},
        'amounts': {'recognized_turnover': turnover, 'recognized_gross_units': units,
                    'verified_fee_amount': known_fees if known_fee_orders else None,
                    'orders_with_known_fees': known_fee_orders,
                    'orders_with_unknown_fees': unknown_fee_orders,
                    'orders_with_unknown_currency': unknown_currency},
        'stake_limit_matrix': stake_matrix,
        'fee_stress_matrix': fee_matrix,
        'profit_slippage_matrix': slippage_matrix,
        'unaccepted_opportunities': 'not_estimable_from_accepted_cohort',
        'assumptions': [
            'amounts use one latest valid receipt per accepted order',
            'fee stress charges the stated rate on capped stake in addition to receipt net',
            'profit slippage reduces only the positive gross part of a unit return',
            'fee and slippage rows are scenario stresses, not verified account costs',
        ],
    }


def _drawdown(realized):
    balance = peak = loss = ZERO
    for at in sorted(realized):
        balance += realized[at]
        peak = max(peak, balance)
        loss = max(loss, peak - balance)
    return loss


def exposure(document):
    """C05: same-fixture open-position replay against full follow."""
    rows = _document_rows(document)
    cutoff = _moment(document['cutoff'])
    ordered = sorted(((_moment(row['placed_at']), row) for row in rows),
                     key=lambda item: (item[0], str(item[1].get('order_id'))))
    results = {}
    for name, cap in POLICIES.items():
        open_by_fixture = {}
        entries, blocked, unknown_principal = [], 0, 0
        peak_open = 0
        peak_principal = ZERO
        fixtures = set()
        for placed, row in ordered:
            for positions in open_by_fixture.values():
                positions[:] = [position for position in positions
                                if not (position['release'] is not None and position['release'] < placed)]
            fixture = row.get('fixture_id')
            held = open_by_fixture.setdefault(fixture, [])
            if cap is not None and len(held) >= cap:
                blocked += 1
                continue
            principal = _source_principal(row)
            if principal is None:
                unknown_principal += 1
            try:
                _, values = _receipts(row)
            except (KeyError, TypeError, ValueError):
                values = []
            first = _first_settled(values, cutoff)
            latest = _latest_valid(values, cutoff)
            entry = {'at': placed, 'principal': principal, 'latest': latest}
            entries.append(entry)
            fixtures.add(fixture)
            held.append({'release': first.observed_at if first is not None else None,
                         'principal': principal})
            peak_open = max(peak_open, sum(len(items) for items in open_by_fixture.values()))
            known = sum((item['principal'] for item in held if item['principal'] is not None), ZERO)
            peak_principal = max(peak_principal, known)

        realized = {}
        settled = pending = 0
        for entry in entries:
            if entry['latest'] is None:
                pending += 1
                continue
            settled += 1
            raw, receipt = entry['latest']
            amount = Decimal(str(raw['stake']))
            units = receipt.unit_return * amount
            realized[receipt.observed_at] = realized.get(receipt.observed_at, ZERO) + units
        results[name] = {
            'entries': len(entries),
            'blocked': blocked,
            'fixtures': len(fixtures),
            'settled_entries': settled,
            'pending_entries': pending,
            'unknown_entry_principal': unknown_principal,
            'known_principal_peak': peak_principal if unknown_principal == 0 else None,
            'known_principal_peak_lower_bound': peak_principal,
            'peak_open_positions': peak_open,
            'realized_units': sum(realized.values(), ZERO),
            'max_drawdown_units': _drawdown(realized),
        }
    return {
        'study': 'C05',
        'denominator': {'rows': len(rows), 'ordered_entries': len(ordered)},
        'release_rule': 'first valid settled receipt observed strictly before the next entry',
        'outcome_rule': 'latest valid settled receipt observed at or before cutoff; conflicting latest versions are excluded',
        'policies': results,
        'assumptions': [
            'entry principal is read from the source row only (stake/principal/source)',
            'missing entry principal stays unknown; receipt stake is never used at entry',
            'same-second release does not free a position for an entry at that instant',
            'blocked entries keep their historical decision and contribute no outcome',
        ],
    }
