"""C06/P06: fee-aware economics, frozen coverage gates and clustered uncertainty.

Both studies reuse the frozen strategy evaluator in ``simulate_strategies`` and the
same immutable receipt versions. ``economics`` lists each independent blocker instead
of collapsing them into one verdict, and prices capacity only where the archive
carries evidence. Missing capacity or cost is a labelled scenario, never zero filled;
an unknown contract payout ceiling stays unknown. ``resampling`` keeps every leg of a
fixture together and compares per-bet, per-fixture and two-day-block resampling on
the same settled returns with a fixed seed.
"""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from math import ceil
from random import Random

from .. import simulate_strategies as simulation
from . import inputs
from .evidence import INSUFFICIENT, MEASURED
from .stats import (
    counter,
    design_effect,
    required_two_point_sd,
    sample_variance,
    two_sided_interval,
)

ZERO = Decimal(0)
SEED = 20261122
ROUNDS = 4000
MARGIN = Decimal('0.02')
BLOCK_DAYS = 2
SCENARIO_CAPACITY_MULTIPLIERS = (Decimal(1), Decimal(2), Decimal(5))
SCENARIO_EXPENSES = (Decimal(0), Decimal(10), Decimal(25), Decimal(50))
ONE_UNIT_STAKE_NOTE = 'one unit of stake per unresolved selected order'


def _number(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _nonnegative(value):
    result = _number(value)
    return result if result is not None and result >= 0 else None


def _positive(value):
    result = _nonnegative(value)
    return result if result is not None and result > 0 else None


def _source(record):
    if isinstance(record, dict):
        for field in ('source', 'evidence', 'reference'):
            value = record.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def capacity_evidence(document):
    """Capacity is only evidenced when a positive quantity carries a source."""
    raw = document.get('capacity')
    if raw is None:
        raw = document.get('capacity_evidence')
    if raw is None:
        records = []
    elif isinstance(raw, list):
        records = raw
    else:
        records = [raw]
    units = ZERO
    sources = []
    unverified = 0
    for record in records:
        if isinstance(record, dict):
            value = _positive(record.get('units', record.get('capacity_units')))
        else:
            value = _positive(record)
        source = _source(record)
        if value is None or source is None:
            unverified += 1
            continue
        units += value
        sources.append(source)
    if units > 0:
        return {'status': 'reported', 'units': units, 'sources': sorted(set(sources)),
                'unverified_records': unverified}
    return {'status': 'missing', 'units': None, 'reason': 'no_capacity_evidence_in_export',
            'unverified_records': unverified}


def expense_assumptions(document):
    """Provided expense evidence first; otherwise an explicit scenario grid."""
    raw = document.get('expense_scenarios')
    if raw is None:
        raw = document.get('expenses')
    records = [] if raw is None else raw if isinstance(raw, list) else [raw]
    rows = []
    for record in records:
        if isinstance(record, dict):
            value = _nonnegative(record.get('units', record.get('amount')))
        else:
            value = _nonnegative(record)
        if value is None:
            continue
        source = _source(record)
        rows.append({'units': value, 'evidence': 'reported' if source else 'scenario',
                     'source': source})
    if rows:
        return rows
    return [{'units': value, 'evidence': 'scenario', 'source': None}
            for value in SCENARIO_EXPENSES]


def payout_ceiling(document):
    """Never infer a contract payout cap from observed payouts."""
    raw = document.get('payout_ceiling')
    if isinstance(raw, dict):
        value = _positive(raw.get('units', raw.get('amount')))
    else:
        value = _positive(raw)
    source = _source(raw)
    if value is not None and source is not None:
        return {'status': 'reported', 'units': value, 'source': source, 'fabricated': False}
    return {'status': 'unknown', 'units': None,
            'reason': 'no_contract_payout_ceiling_in_export', 'fabricated': False}


def _registered(document):
    registered = document.get('registered_at')
    if not registered:
        return None, None
    return inputs.utc(registered), document.get('primary_arm')


def _cohort(document):
    begin, end, cutoff = inputs.interval(document)
    registered_at, arm = _registered(document)
    result = simulation.run(document, begin, end, registered_at=registered_at, primary_arm=arm)
    key = arm or 'baseline'
    rows = document['rows']
    fills = [simulation.Fill.parse(row) for row in rows
             if row.get('fixture_id') and row.get('market_kind') and row.get('placed_at')]
    chosen = simulation.choose(fills, begin, end)[key]
    trades = []
    for fill in chosen:
        receipt = fill.latest_receipt(cutoff)
        if receipt is not None and receipt.unit_return is not None:
            trades.append((fill, receipt))
    return {'begin': begin, 'end': end, 'cutoff': cutoff, 'arm': key,
            'primary_arm': arm, 'registered_at': registered_at,
            'evaluator': result, 'summary': result['arms'][key],
            'chosen': chosen, 'trades': trades}


def _weeks(context):
    return simulation.interval_summary(context['trades'], context['begin'], context['end'],
                                       simulation.WEEK, use_settlement=False)


def _sensitivity(capacity, expenses, marginal, settled):
    if marginal is None or not settled:
        return {'evidence_label': INSUFFICIENT, 'reason': 'no_settled_returns', 'rows': []}
    if capacity['status'] == 'reported':
        capacities = [(capacity['units'], 'reported_capacity')]
    else:
        capacities = [(settled * multiplier, f'scenario_observed_settled_x{multiplier.normalize()}')
                      for multiplier in SCENARIO_CAPACITY_MULTIPLIERS]
    rows = []
    for units, capacity_basis in capacities:
        for expense in expenses:
            cost = expense['units']
            contribution = units * marginal
            net = contribution - cost
            rows.append({
                'capacity_units': units,
                'capacity_basis': capacity_basis,
                'assumed_expense_units': cost,
                'expense_basis': expense['evidence'],
                'marginal_return_per_unit': marginal,
                'contribution_units': contribution,
                'net_after_assumed_expense': net,
                'break_even_marginal_return': cost / units if units else None,
                'break_even_capacity_units': cost / marginal if marginal > 0 else None,
                'profitable_under_assumption': net > 0,
            })
    scenario = capacity['status'] != 'reported' or any(row['expense_basis'] == 'scenario' for row in rows)
    return {'evidence_label': 'scenario' if scenario else MEASURED,
            'results_are_scenarios': scenario,
            'note': 'missing capacity and assumed expenses are scenarios, not archive facts',
            'rows': rows}


def economics(document):
    """C06: independent gates, pessimistic pending bound and capacity economics."""
    context = _cohort(document)
    summary = context['summary']
    trades = context['trades']
    chosen = context['chosen']
    settled = len(trades)
    missing_fees = sum(receipt.unit_fees is None for _, receipt in trades)
    missing_currency = sum(receipt.currency is None for _, receipt in trades)
    missing_settlement = sum(receipt.settled_at is None for _, receipt in trades)
    currencies = sorted({receipt.currency for _, receipt in trades if receipt.currency})
    fixtures = len({fill.fixture for fill, _ in trades})
    net = sum((receipt.stressed_return for _, receipt in trades), ZERO)
    gross = sum((receipt.unit_return for _, receipt in trades), ZERO)
    roi = net / settled if settled else None
    drawdown = simulation.drawdown(trades)
    weeks = _weeks(context)
    complete = [week for week in weeks if week['complete']]
    positive = [week for week in complete if week['stressed_units'] > 0]
    unsettled = len(chosen) - settled
    pessimistic = net - Decimal(unsettled)
    capacity = capacity_evidence(document)
    expenses = expense_assumptions(document)
    sensitivity = _sensitivity(capacity, expenses, roi, settled)
    consistent = summary['settled'] == settled and summary['stressed_units'] == net
    gaps = {
        'settled_receipts': {
            'state': 'met' if settled else 'not_met',
            'selected': len(chosen), 'settled': settled,
        },
        'missing_fees': {
            'state': 'met' if missing_fees == 0 else 'not_met',
            'missing': missing_fees, 'settled': settled,
        },
        'missing_currency': {
            'state': 'met' if missing_currency == 0 else 'not_met',
            'missing': missing_currency, 'settled': settled,
        },
        'mixed_currencies': {
            'state': 'met' if len(currencies) <= 1 else 'not_met',
            'currencies': currencies,
        },
        'maximum_drawdown': {
            'state': 'reported',
            'units': drawdown,
        },
        'negative_return': {
            'state': 'met' if net > 0 else 'not_met',
            'stressed_units': net,
        },
        'fewer_than_200_fixtures': {
            'state': 'met' if fixtures >= 200 else 'not_met',
            'fixtures': fixtures,
        },
        'four_positive_complete_weeks': {
            'state': 'met' if len(complete) >= 4 and len(positive) == len(complete) else 'not_met',
            'complete_weeks': len(complete), 'positive_complete_weeks': len(positive),
        },
        'unsettled_receipts': {
            'state': 'met' if unsettled == 0 else 'not_met',
            'unsettled': unsettled,
            'pessimistic_units': pessimistic,
            'pessimistic_assumption': ONE_UNIT_STAKE_NOTE,
        },
    }
    measured = settled > 0 and missing_fees == 0 and missing_currency == 0 and len(currencies) == 1
    return {
        'experiment': 'C06',
        'evidence_label': MEASURED if measured else INSUFFICIENT,
        'window': {'begin': context['begin'], 'end': context['end'], 'cutoff': context['cutoff']},
        'arm': context['arm'],
        'registered_at': context['registered_at'],
        'gaps': gaps,
        'receipts': {
            'selected': len(chosen), 'settled': settled,
            'missing_fees': missing_fees, 'missing_currency': missing_currency,
            'missing_settlement_time': missing_settlement, 'unsettled': unsettled,
            'currencies': currencies,
        },
        'returns': {
            'receipt_units_before_listed_fees': gross,
            'stressed_units': net,
            'stressed_roi': roi,
            'lower_95_roi': summary['lower_95_roi'],
            'maximum_drawdown': drawdown,
            'pessimistic_units': pessimistic,
            'pessimistic_assumption': ONE_UNIT_STAKE_NOTE,
        },
        'capacity': capacity,
        'expense_assumptions': expenses,
        'payout_ceiling': payout_ceiling(document),
        'sensitivity': sensitivity,
        'frozen_evaluator': {
            'settled': summary['settled'],
            'stressed_units': summary['stressed_units'],
            'lower_95_roi': summary['lower_95_roi'],
            'validation_blockers': summary['validation_blockers'],
            'consistent_with_recount': consistent,
        },
    }


def _bootstrap_means(groups, rng, rounds=ROUNDS):
    """Resample whole groups with replacement and average pooled unit returns."""
    count = len(groups)
    if not count:
        return []
    means = []
    for _ in range(rounds):
        total = ZERO
        units = 0
        for _ in range(count):
            group = groups[rng.randrange(count)]
            total += sum(group, ZERO)
            units += len(group)
        means.append(total / units if units else ZERO)
    return means


def _sqrt(value):
    if value is None or value < 0:
        return None
    return value.sqrt()


def _effective(samples, effect):
    if effect is None or effect <= 0:
        return None
    return Decimal(samples) / effect


def _two_day_groups(trades, begin, end):
    """Assign every fixture to one two-day block by its earliest block time."""
    span_seconds = (end - begin).total_seconds()
    block_count = max(1, ceil(span_seconds / (BLOCK_DAYS * 86400)))
    block_of = {}
    for fill, receipt in trades:
        at = receipt.settled_at or fill.placed_at
        index = int((at - begin).total_seconds() // (BLOCK_DAYS * 86400))
        index = min(max(index, 0), block_count - 1)
        previous = block_of.get(fill.fixture)
        block_of[fill.fixture] = index if previous is None else min(previous, index)
    groups = [[] for _ in range(block_count)]
    for fill, receipt in trades:
        groups[block_of[fill.fixture]].append(receipt.stressed_return)
    return groups


def resampling(document):
    """P06: per-bet, per-fixture and two-day-block intervals on the same returns."""
    context = _cohort(document)
    trades = context['trades']
    values = [receipt.stressed_return for _, receipt in trades]
    window = {'begin': context['begin'], 'end': context['end'], 'cutoff': context['cutoff']}
    base = {
        'experiment': 'P06',
        'seed': SEED,
        'rounds': ROUNDS,
        'block_days': BLOCK_DAYS,
        'arm': context['arm'],
        'window': window,
    }
    if not values:
        return base | {
            'evidence_label': INSUFFICIENT,
            'settled': 0,
            'missing_reasons': ['empty_settled_sample'],
            'intervals': {'per_bet': None, 'per_fixture': None, 'two_day_block': None},
            'design_effect': {'per_fixture': None, 'two_day_block': None},
            'effective_samples': {'per_fixture': None, 'two_day_block': None},
            'clusters': {'fixtures': 0, 'two_day_blocks': 0, 'nonempty_blocks': 0},
            'identification_2pp': None,
        }

    by_fixture = defaultdict(list)
    for fill, receipt in trades:
        by_fixture[fill.fixture].append(receipt.stressed_return)
    fixture_groups = [by_fixture[key] for key in sorted(by_fixture)]
    block_groups = _two_day_groups(trades, context['begin'], context['end'])

    rng = Random(SEED)
    per_bet = _bootstrap_means([[value] for value in values], rng)
    per_fixture = _bootstrap_means(fixture_groups, rng)
    two_day = _bootstrap_means(block_groups, rng)
    unit_var = sample_variance(per_bet)
    fixture_var = sample_variance(per_fixture)
    block_var = sample_variance(two_day)
    fixture_effect = design_effect(fixture_var, unit_var)
    block_effect = design_effect(block_var, unit_var)
    observed_var = sample_variance(values)
    degenerate = observed_var is None or observed_var == 0 or unit_var is None or unit_var == 0

    fixture_means = [sum(group, ZERO) / len(group) for group in fixture_groups]
    per_bet_sd = _sqrt(observed_var)
    fixture_sd = _sqrt(sample_variance(fixture_means))
    independent_bets = required_two_point_sd(per_bet_sd, MARGIN)
    independent_fixtures = required_two_point_sd(fixture_sd, MARGIN)
    identification = {
        'margin': MARGIN,
        'assumption': 'independent per-bet returns, two-sided 95% interval, observed SD',
        'per_bet_sd': per_bet_sd,
        'independent_bets': independent_bets,
        'fixture_mean_sd': fixture_sd,
        'independent_fixtures': independent_fixtures,
        'fixture_design_effect': fixture_effect,
        'design_adjusted_bets': (independent_bets * fixture_effect
                                 if independent_bets is not None and fixture_effect is not None else None),
        'design_adjusted_fixtures': (independent_fixtures * fixture_effect
                                     if independent_fixtures is not None and fixture_effect is not None else None),
        'design_effect_applied': fixture_effect is not None,
    }
    fixture_legs = counter([len(group) for group in fixture_groups])
    return base | {
        'evidence_label': MEASURED,
        'settled': len(values),
        'missing_reasons': ['degenerate_variance'] if degenerate else [],
        'degenerate_variance': degenerate,
        'mean_return_per_unit': sum(values, ZERO) / len(values),
        'intervals': {
            'per_bet': two_sided_interval(per_bet),
            'per_fixture': two_sided_interval(per_fixture),
            'two_day_block': two_sided_interval(two_day),
        },
        'design_effect': {'per_fixture': fixture_effect, 'two_day_block': block_effect},
        'effective_samples': {
            'per_fixture': _effective(len(fixture_groups), fixture_effect),
            'two_day_block': _effective(len(block_groups), block_effect),
        },
        'clusters': {
            'fixtures': len(fixture_groups),
            'fixture_leg_counts': fixture_legs,
            'observed_bets_per_fixture': len(values) / len(fixture_groups),
            'two_day_blocks': len(block_groups),
            'nonempty_blocks': sum(1 for group in block_groups if group),
            'fixture_legs_kept_together': True,
        },
        'identification_2pp': identification,
    }
