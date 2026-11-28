"""C03: expert edge relative to the market, wrapping the frozen replay.

The study reuses ``market_adjusted_experts.replay`` unchanged for the residual
comparison and adds only read-only audit layers: settlement-version visibility,
half-unit effective principal, two-cohort comparison and explicit marks for
"forecast ability" versus "post-fee tradeable". The archive has no fee or
currency evidence, so post-fee tradability stays unknown instead of being
inferred from a stress assumption.
"""

from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from . import inputs
from .evidence import INSUFFICIENT, MEASURED, load_json, section

STUDY = 'C03'
MARKET_FILE = 'market-adjusted-cohort.json'
ACTIVE_STAKES = {
    'win': Decimal(1), 'half_win': Decimal('0.5'),
    'loss': Decimal(1), 'half_loss': Decimal('0.5'),
}
PASSIVE_OUTCOMES = {'push', 'void'}
ZERO = Decimal(0)
QUANTUM = Decimal('0.000001')


def _replay(document, split):
    from market_adjusted_experts import replay

    return replay(document, split)


def stake_audit(document, cutoff):
    """Audit the latest visible result version and its effective principal.

    A version observed at or before the recommendation creation time, or at or
    after the cutoff, is not usable. Half-win and half-loss must use half a unit
    of effective principal; push and void must use none.
    """
    issues = Counter()
    statuses = Counter()
    half_stakes = Counter()
    for row in document.get('rows', ()):
        created = inputs.utc(row['created_at'])
        versions = row.get('versions') or []
        eligible = []
        for version in versions:
            observed = inputs.utc(version['observed_at'])
            if observed <= created:
                issues['version_not_after_creation'] += 1
            elif observed >= cutoff:
                issues['version_at_or_after_cutoff'] += 1
            else:
                eligible.append((observed, version))
        if not eligible:
            issues['no_visible_settlement_version'] += 1
            continue
        observed, latest = eligible[-1]
        statuses[latest.get('status')] += 1
        if latest.get('status') not in {'confirmed', 'void'}:
            issues['latest_version_not_final'] += 1
            continue
        records = [
            record for record in latest.get('records') or ()
            if observed <= inputs.utc(record['computed_at']) < cutoff
        ]
        if not records:
            issues['no_visible_ledger_record'] += 1
            continue
        record = records[-1]
        outcome = record.get('outcome')
        if outcome in ACTIVE_STAKES:
            expected_stake = ACTIVE_STAKES[outcome]
        elif outcome in PASSIVE_OUTCOMES:
            expected_stake = ZERO
        else:
            issues['unknown_outcome'] += 1
            continue
        try:
            stake = Decimal(record['stake'])
            net = None if record.get('net') is None else Decimal(record['net'])
        except (ArithmeticError, TypeError, ValueError):
            issues['unreadable_ledger_amount'] += 1
            continue
        if stake != expected_stake:
            issues['effective_stake_mismatch'] += 1
        if outcome in {'half_win', 'half_loss'}:
            half_stakes[outcome] += 1
        price = Decimal(row['recommendation_price'])
        won = stake if outcome.endswith('win') else ZERO
        expected_net = (won * price - stake).quantize(QUANTUM, rounding=ROUND_HALF_UP)
        if net is None or net != expected_net:
            issues['net_does_not_reconcile'] += 1
    return {
        'recommendations': len(document.get('rows', ())),
        'latest_version_statuses': dict(sorted(statuses.items())),
        'half_unit_settlements': dict(sorted(half_stakes.items())),
        'issues': dict(sorted(issues.items())),
    }


def compare_cohorts(replay):
    """Earlier/later cohort difference for every retained strategy."""
    arms = ('follow_all_priced', 'market_adjusted', 'market_adjusted_conservative')
    fields = ('selected', 'settled', 'fixtures', 'stressed_quote_units',
              'stressed_quote_units_without_top_market')
    comparison = {}
    for arm in arms:
        earlier = replay['cohorts']['earlier'].get(arm, {})
        later = replay['cohorts']['later'].get(arm, {})
        comparison[arm] = {
            'earlier': {field: earlier.get(field) for field in fields},
            'later': {field: later.get(field) for field in fields},
            'later_minus_earlier_stressed_quote_units': (
                later.get('stressed_quote_units', ZERO) - earlier.get('stressed_quote_units', ZERO)
            ),
        }
    return comparison


def run(cohort_dir, split, root=None):
    """C03 entry point: replay the frozen market-adjusted cohort at ``split``."""
    directory = Path(cohort_dir)
    document, record = load_json(directory / MARKET_FILE, root or directory)
    begin, end, cutoff = inputs.interval(document)
    split = inputs.utc(split)
    if not begin < split < end:
        raise ValueError('split must be inside the frozen evidence interval')

    audit = stake_audit(document, cutoff)
    missing = Counter({
        'fees_and_currency_absent_from_archive': 1,
        'quotes_are_historical_not_executable': 1,
        'stress_cost_is_an_assumption_not_account_cost': 1,
    })
    try:
        replay = _replay(document, split)
    except Exception as error:  # noqa: BLE001 - report an unavailable frozen evaluator
        return section(
            label=INSUFFICIENT,
            sources=[record],
            missing=[f'replay_unavailable: {type(error).__name__}: {error}'],
            coverage={'recommendations': len(document.get('rows', ()))},
            results={'stake_audit': audit, 'forecast_ability': 'insufficient_evidence',
                     'post_fee_tradeable': 'unknown'},
        )

    settled = sum(
        replay['cohorts'][cohort][arm]['settled']
        for cohort in ('earlier', 'later')
        for arm in ('follow_all_priced',)
    )
    return section(
        label=MEASURED if settled else INSUFFICIENT,
        sources=[record],
        missing=missing,
        coverage={
            'recommendations': len(document.get('rows', ())),
            'follow_all_settled': settled,
            'cohorts': {cohort: dict(sorted(counts.items()))
                        for cohort, counts in replay.get('coverage', {}).items()},
        },
        results={
            'split': split.isoformat(),
            'replay': replay,
            'stake_audit': audit,
            'cohort_comparison': compare_cohorts(replay),
            'marks': {
                'forecast_ability': 'measured_on_historical_quote_residual' if settled
                else 'insufficient_settled_evidence',
                'post_fee_tradeable': 'unknown_missing_fee_and_currency_evidence',
                'validation_eligible': False,
                'stress_cost_applied_to_quotes_only': str(replay.get('assumed_stress')),
            },
        },
        limitations=[
            'historical quotes and residual returns are not executable account costs',
            'no fee or currency evidence exists in this archive; post-fee profitability is unknown',
            'settlements observed at or after cutoff and same-second priors are excluded',
        ],
    )
