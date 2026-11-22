"""C02: pricing cost, certified cost and dual-source evidence.

The study consumes the frozen ``paper_validate`` / ``preflight_price_decision``
outputs when they are available and otherwise reads the status fields already
present on the paper frames. Three facts stay independent:

* the native selected price against the recommendation price;
* whether a certified account cost exists (cost is never assumed zero);
* whether two complete independent sources and settlement evidence exist.

A price below the minimum and a missing piece of evidence are different
outcomes. Nothing here re-runs the pricing rule or treats a missing cost as a
free trade.
"""

from collections import Counter
from datetime import timedelta
from decimal import Decimal
import json

from infra.profitability.experiments import inputs
from infra.profitability.experiments.clock import PAPER_PREFLIGHT_MAX_SECONDS
from infra.profitability.experiments.stats import counter, quantile, stratified

STUDY = 'C02'
THRESHOLD_SECONDS = Decimal(30)
PRICED = 'price_eligible'
BELOW = 'below_minimum_odds'


def _price(row, key='native_selected_price'):
    return inputs.number(row.get(key))


def _native_prices(row):
    values = row.get('native_prices')
    if not isinstance(values, dict) or len(values) < 2:
        return None
    parsed = {name: inputs.number(value) for name, value in values.items()}
    if any(value is None or value <= 1 for value in parsed.values()):
        return None
    return parsed


def _decision(decisions, signal_id):
    if not isinstance(decisions, dict):
        return None
    return decisions.get(signal_id)


def _certified_cost(row, decision, cost_root):
    from infra.profitability.paper_validate import account_cost

    return bool(cost_root is not None and decision and 'account_cost_approval' in decision
                and account_cost(row, decision, cost_root) is not None)


def _dual_sources(decision):
    if decision:
        sources = decision.get('complete_sources') or []
        return len(sources) >= 2 and decision.get('status') in (PRICED, BELOW)
    return False


def _timing(row):
    """Validate the frozen quote and paper-preflight clock for one row."""
    try:
        created = inputs.utc(row['created_at'])
        quoted = inputs.utc(row['quote_time'])
    except (KeyError, TypeError, ValueError):
        return 'invalid_quote_time'
    if quoted > created:
        return 'quote_after_created'
    try:
        observed = inputs.utc(row['_observed'])
        checked = inputs.utc(row['native_quote_checked_at'])
    except (KeyError, TypeError, ValueError):
        return 'missing_preflight_check'
    if not observed <= checked <= observed + timedelta(seconds=PAPER_PREFLIGHT_MAX_SECONDS):
        return 'preflight_check_outside_window'
    return None


def analyze(frames, decisions=None, cutoff=None, *, reference_root=None, cost_root=None):
    from infra.profitability.paper_validate import price_decisions, validate_offer

    decisions = decisions if isinstance(decisions, dict) else {}
    rows = []
    problems = Counter()
    offers = {}
    for frame in frames:
        clock = inputs.frame_clock(frame)
        for name in clock['problems']:
            problems[name] += 1
        if 'slot' not in clock or clock['problems']:
            continue
        for row in frame.get('rows') or []:
            if row.get('status') != 'native_preflight_available' and row.get('signal_id') not in decisions:
                continue
            signal_id = row.get('signal_id')
            if not signal_id or signal_id in offers:
                raise ValueError('missing or duplicate paper signal')
            rows.append(row | {'_observed': clock['observed']})
            offers[signal_id] = None
            if reference_root is not None:
                try:
                    offers[signal_id] = validate_offer(row, frame, cutoff)
                except (KeyError, TypeError, ValueError, ArithmeticError):
                    problems['invalid_frozen_offer'] += 1

    reference_gaps = Counter()
    if reference_root is not None:
        manifest = json.loads((reference_root / 'reference-price.json').read_bytes())
        decisions, _ = price_decisions({k: v for k, v in offers.items() if v is not None},
                                      reference_root, manifest['fixtures'])
    for decision in decisions.values():
        reference_gaps.update(decision.get('reasons', ()))

    classes = Counter()
    cost_counts = Counter()
    source_counts = Counter()
    native_comparison, strata = [], []
    unusable = Counter()
    for row in rows:
        signal_id = row.get('signal_id')
        decision = _decision(decisions, signal_id)
        timing = _timing(row)
        if timing:
            unusable[timing] += 1
            classes['unusable_quote'] += 1
            continue
        if cutoff is not None:
            try:
                checked = inputs.utc(row['native_quote_checked_at'])
            except (KeyError, TypeError, ValueError):
                unusable['missing_preflight_check'] += 1
                classes['unusable_quote'] += 1
                continue
            if checked > cutoff:
                unusable['preflight_after_cutoff'] += 1
                classes['unusable_quote'] += 1
                continue
        cost = _certified_cost(row, decision, cost_root)
        dual = _dual_sources(decision)
        cost_counts['certified' if cost else 'missing'] += 1
        source_counts['dual_complete' if dual else 'missing'] += 1
        price = _price(row)
        complete_market = _native_prices(row)
        if price is None or price <= 1 or complete_market is None:
            classes['invalid_native_price'] += 1
            continue
        status = (decision or {}).get('status')
        if status == BELOW and cost and dual:
            classes[BELOW] += 1
        elif status == PRICED and cost and dual:
            classes[PRICED] += 1
        elif cost and dual:
            classes['evidence_ineligible'] += 1
        elif not cost and not dual:
            classes['missing_cost_and_sources'] += 1
        elif not cost:
            classes['missing_cost'] += 1
        else:
            classes['missing_dual_sources'] += 1
        recommendation = _price(row, 'recommendation_price')
        if recommendation is None or recommendation <= 1:
            continue
        delta = price - recommendation
        native_comparison.append({
            'sport': row.get('sport'),
            'phase': row.get('phase'),
            'market_kind': row.get('market_kind'),
            'delta': delta,
        })
        strata.append({'sport': row.get('sport'), 'phase': row.get('phase'),
                       'market_kind': row.get('market_kind'), 'delta': delta})

    def measure(bucket):
        delta = [item['delta'] for item in bucket]
        return {
            'rows': len(bucket),
            'lower': sum(value < 0 for value in delta),
            'equal': sum(value == 0 for value in delta),
            'higher': sum(value > 0 for value in delta),
            'median_delta': quantile(delta, Decimal('0.5')),
        }

    return {
        'experiment': STUDY,
        'evidence_label': 'historical_pricing' if classes[PRICED] or classes[BELOW] else 'insufficient_evidence',
        'decisions_supplied': len(decisions),
        'denominator': {'native_preflight_rows': len(rows), 'priced_or_classified': len(rows) - classes['unusable_quote']},
        'classes': counter(classes.elements()),
        'unusable': dict(sorted(unusable.items())),
        'cost_evidence': dict(sorted(cost_counts.items())),
        'source_evidence': dict(sorted(source_counts.items())),
        'problems': dict(sorted(problems.items())),
        'reference_reasons': dict(reference_gaps),
        'native_price_comparison': {
            'status': 'measured' if native_comparison else 'insufficient_evidence',
            'rows': len(native_comparison),
            'delta': measure(native_comparison),
            'strata': stratified(strata, ('sport', 'phase', 'market_kind'), measure),
        },
        'note': 'certified cost and dual-source status are reported separately from the native price comparison',
    }
