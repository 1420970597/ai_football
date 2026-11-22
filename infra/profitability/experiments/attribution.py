"""P01: paper-opportunity loss attribution on the minute archive.

Rows are grouped by the actual rejection code, phase, contract and league.
The archive has no league field, so a missing league is reported as the
explicit value ``unknown`` instead of being dropped or guessed. Every cell
carries its own group denominator and every UTC day keeps a separate
denominator.

The 30/45/60-second counterfactual is only evaluated when the archive also
stores the same opportunity's state over time. Without that history the
counterfactual is reported as not estimable; a missing quote is never blamed
on latency by assumption.
"""

from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from infra.profitability.experiments import inputs
from infra.profitability.experiments.stats import counter, cross_tab, unique

STUDY = 'P01'
HORIZONS = (30, 45, 60)
ONE_MINUTE = timedelta(minutes=1)
DIMENSIONS = ('phase', 'market_kind', 'period', 'line', 'selection', 'league')
ACCEPTED = 'native_preflight_available'


def _reason(row):
    return row.get('status') or 'unknown_status'


def _league(row):
    return row.get('league') or row.get('competition') or 'unknown'


def _line(row):
    value = row.get('line')
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return str(Decimal(str(value)).normalize())
    except (InvalidOperation, TypeError, ValueError):
        return 'invalid'


def _counterfactual(rows):
    histories = [row for row in rows if isinstance(row.get('native_state_history'), list)]
    if not histories:
        return {
            'status': 'not_estimable',
            'reason': 'no_same_opportunity_state_history',
            'horizons_seconds': list(HORIZONS),
        }
    recovered = dict.fromkeys(HORIZONS, 0)
    evaluable = 0
    for row in histories:
        try:
            created = inputs.utc(row['created_at'])
        except (KeyError, TypeError, ValueError):
            continue
        states = []
        for state in row['native_state_history']:
            try:
                at = inputs.utc(state['at'])
            except (KeyError, TypeError, ValueError):
                continue
            states.append((at, state.get('status'), state.get('price')))
        if not states:
            continue
        evaluable += 1
        for horizon in HORIZONS:
            if any(created < at <= created + timedelta(seconds=horizon)
                   and status == ACCEPTED and price is not None
                   for at, status, price in states):
                recovered[horizon] += 1
    return {
        'status': 'measured',
        'evaluable_opportunities': evaluable,
        'recovered_by_horizon': {str(key): value for key, value in recovered.items()},
        'recovered_share_by_horizon': {
            str(key): (Decimal(value) / evaluable if evaluable else None) for key, value in recovered.items()
        },
        'horizons_seconds': list(HORIZONS),
    }


def analyze(frames, start=None, end=None):
    problems = Counter()
    slots, raw_rows = [], []
    for frame in frames:
        clock = inputs.frame_clock(frame)
        for name in clock['problems']:
            problems[name] += 1
        if 'slot' not in clock or clock['problems']:
            continue
        slots.append(clock['slot'])
        for row in frame.get('rows') or []:
            if not row.get('signal_id'):
                problems['missing_signal_identifier'] += 1
                continue
            raw_rows.append(row)

    rows, duplicate_rows = unique(raw_rows, 'signal_id')
    enriched = []
    for row in rows:
        enriched.append(dict(row) | {
            'phase': row.get('phase') or 'unknown',
            'market_kind': row.get('market_kind') or 'unknown',
            'period': row.get('period') or 'unknown',
            'line': _line(row),
            'selection': row.get('selection') or 'unknown',
            'league': _league(row),
            'reason': _reason(row),
        })

    if start is None and slots:
        start = min(slots)
    if end is None and slots:
        end = max(slots) + ONE_MINUTE
    if start is not None and end is not None:
        expected = int((end - start) // ONE_MINUTE)
        distinct = {slot for slot in slots if start <= slot < end}
        interval = {'start': start, 'end': end, 'expected_slots': expected,
                    'observed_slots': len(distinct), 'missing_slots': expected - len(distinct),
                    'capture_share': Decimal(len(distinct)) / expected if expected else None}
    else:
        interval = {'start': None, 'end': None}

    windows = defaultdict(Counter)
    for row in enriched:
        try:
            day = inputs.utc(row['created_at']).date().isoformat()
        except (KeyError, TypeError, ValueError):
            day = 'unknown'
        windows[day][row['reason']] += 1

    table = cross_tab(enriched, DIMENSIONS, 'reason')
    return {
        'experiment': STUDY,
        'interval': interval,
        'denominator': {
            'frames': len(frames),
            'archived_rows': len(raw_rows),
            'unique_opportunities': len(rows),
            'duplicate_rows': duplicate_rows,
            'league_unknown': sum(row['league'] == 'unknown' for row in enriched),
        },
        'problems': dict(sorted(problems.items())),
        'reasons': counter([row['reason'] for row in enriched]),
        'cross_tab': table,
        'windows': [
            {'day': day, 'denominator': sum(counts.values()), 'reasons': dict(sorted(counts.items()))}
            for day, counts in sorted(windows.items())
        ],
        'counterfactual': _counterfactual(rows),
    }
