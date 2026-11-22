"""C01: recommendation, preflight and native-entry clock audit.

Two independent queues are reported because the frozen order export and the
minute archive share no stable identifier. Delays are measured inside each
queue only; a single record is never reconstructed across queues.
"""

from collections import Counter
from datetime import timedelta
from decimal import Decimal

from infra.profitability.experiments import inputs
from infra.profitability.experiments.stats import (
    counter,
    delays,
    seconds,
    stratified,
    unique,
)

STUDY = 'C01'
THRESHOLD_SECONDS = Decimal(30)


def _delay(start, stop):
    if start is None or stop is None:
        return None
    return Decimal(str(seconds(start, stop)))


def _tally(problems):
    return counter([name for name, count in problems.items() for _ in range(count)])


def frozen_orders(rows, cutoff):
    """Signal -> preflight -> native entry chain on accepted orders."""
    problems = Counter()
    complete, to_preflight, to_entry = [], [], []
    strata_rows = []
    for row in rows:
        try:
            signal = inputs.utc(row['signal_at'])
            preflight = inputs.utc(row['preflight_at'])
            placed = inputs.utc(row['placed_at'])
        except (KeyError, TypeError, ValueError):
            problems['missing_timepoint'] += 1
            continue
        if signal > preflight:
            problems['signal_after_preflight'] += 1
            continue
        if preflight > placed:
            problems['preflight_after_entry'] += 1
            continue
        if placed >= cutoff:
            problems['entry_at_or_after_cutoff'] += 1
            continue
        complete.append(row)
        to_preflight.append(_delay(signal, preflight))
        to_entry.append(_delay(preflight, placed))
        strata_rows.append({
            'sport': row.get('sport'),
            'market_kind': row.get('market_kind'),
            'to_preflight': _delay(signal, preflight),
            'to_entry': _delay(preflight, placed),
        })

    def measure(bucket):
        return {
            'orders': len(bucket),
            'to_preflight': delays([item['to_preflight'] for item in bucket], THRESHOLD_SECONDS),
            'to_entry': delays([item['to_entry'] for item in bucket], THRESHOLD_SECONDS),
        }

    return {
        'denominator': {
            'exported_orders': len(rows),
            'complete_clock': len(complete),
            'excluded': len(rows) - len(complete),
        },
        'problems': _tally(problems),
        'to_preflight': delays(to_preflight, THRESHOLD_SECONDS),
        'to_entry': delays(to_entry, THRESHOLD_SECONDS),
        'strata': stratified(strata_rows, ('sport', 'market_kind'), measure),
    }


def minute_archive(frames):
    """Archive boundary, duplicate and sampling coverage for minute slots."""
    problems = Counter()
    slots, observed, rows = [], [], []
    for frame in frames:
        clock = inputs.frame_clock(frame)
        problems.update(clock['problems'])
        if 'slot' not in clock:
            problems['unusable_frame'] += 1
            continue
        slots.append(clock['slot'])
        observed.append(clock['observed'])
        for row in frame.get('rows') or []:
            rows.append(row | {'_observed': clock['observed']})

    slots.sort()
    distinct = set(slots)
    if slots:
        span = (slots[-1] - slots[0]) // timedelta(minutes=1) + 1
    else:
        span = 0

    kept, duplicate_rows = unique(rows, 'signal_id')
    to_quote, to_check, check_to_observed, to_observed = [], [], [], []
    strata_rows = []
    invalid_times = future_rows = 0
    for row in kept:
        try:
            created = inputs.utc(row['created_at'])
            quoted = inputs.utc(row['quote_time'])
        except (KeyError, TypeError, ValueError):
            invalid_times += 1
            continue
        seen = row['_observed']
        if created > seen:
            future_rows += 1
            continue
        checked = None
        checked_value = row.get('native_quote_checked_at')
        if checked_value:
            try:
                checked = inputs.utc(checked_value)
            except (TypeError, ValueError):
                invalid_times += 1
                continue
            if checked > seen:
                future_rows += 1
                continue
        to_quote.append(_delay(created, quoted))
        to_observed.append(_delay(created, seen))
        if checked:
            to_check.append(_delay(created, checked))
            check_to_observed.append(_delay(checked, seen))
        strata_rows.append({
            'sport': row.get('sport'),
            'phase': row.get('phase'),
            'market_kind': row.get('market_kind'),
            'to_quote': _delay(created, quoted),
            'to_check': _delay(created, checked),
        })

    def measure(bucket):
        return {
            'rows': len(bucket),
            'to_quote': delays([item['to_quote'] for item in bucket], THRESHOLD_SECONDS),
            'to_check': delays([item['to_check'] for item in bucket], THRESHOLD_SECONDS),
        }

    return {
        'denominator': {
            'frames': len(frames),
            'distinct_slots': len(distinct),
            'expected_slots': span,
            'missing_slots': span - len(distinct),
            'capture_share': Decimal(len(distinct)) / span if span else None,
            'archived_rows': len(rows),
            'unique_rows': len(kept),
            'duplicate_rows': duplicate_rows,
        },
        'problems': _tally(problems),
        'invalid_timepoints': invalid_times,
        'future_observations': future_rows,
        'statuses': counter([row.get('status') for row in kept]),
        'to_quote': delays(to_quote, THRESHOLD_SECONDS),
        'to_check': delays(to_check, THRESHOLD_SECONDS),
        'check_to_observed': delays(check_to_observed, THRESHOLD_SECONDS),
        'to_observed': delays(to_observed, THRESHOLD_SECONDS),
        'strata': stratified(strata_rows, ('sport', 'phase', 'market_kind'), measure),
    }


def analyze(native_document, frames):
    begin, end, cutoff = inputs.interval(native_document)
    orders = frozen_orders(inputs.rows(native_document), cutoff)
    archive = minute_archive(frames)
    return {
        'experiment': STUDY,
        'threshold_seconds': THRESHOLD_SECONDS,
        'interval': {'begin': begin, 'end': end, 'cutoff': cutoff},
        'queues': {'frozen_orders': orders, 'minute_archive': archive},
        'cross_queue': {
            'common_identifier': None,
            'matched_opportunities': 0,
            'note': 'order and signal identifiers are disjoint; no record is reconstructed across queues',
        },
        'coverage': {
            'included': {
                'orders': orders['denominator']['complete_clock'],
                'archive_rows': archive['denominator']['unique_rows'],
            },
            'excluded': {
                'orders': orders['denominator']['excluded'],
                'duplicate_archive_rows': archive['denominator']['duplicate_rows'],
                'missing_slots': archive['denominator']['missing_slots'],
            },
            'unknown': {'signal_to_preflight_bridge': 'no common identifier'},
        },
    }
