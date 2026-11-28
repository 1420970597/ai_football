"""C01: recommendation, preflight and native-entry clock audit.

Two independent queues are reported because the frozen order export and the
minute archive share no stable identifier. Delays are measured inside each
queue only; a single record is never reconstructed across queues.

Clock rules fixed by the capture/projection protocols:

* a minute frame is usable only when its slot and boundaries match
  ``begin = slot - 90s``, ``end = slot - 30s`` and ``observed_at`` falls in
  ``[slot, slot + 30s)``;
* a paper preflight is normally observed at or after ``frame.observed_at`` and
  at most 15 seconds later; a check before the observation or beyond that
  window is an invalid timepoint, not a negative delay;
* ``quote_time`` is the upstream source time and normally precedes
  ``created_at``. The quote metric therefore uses the **source age**
  ``created_at - quote_time`` instead of a negative ``created -> quote`` gap;
  the separate ``quote_to_observation`` age keeps the frame boundary visible;
* expected minutes come from the fixed declared interval, never from the
  observed first/last slot, so a truncated archive shows its missing slots.
"""

from collections import Counter, defaultdict
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
PAPER_PREFLIGHT_MAX_SECONDS = 15
ONE_MINUTE = timedelta(minutes=1)


def _delay(start, stop):
    if start is None or stop is None:
        return None
    return Decimal(str(seconds(start, stop)))


def _tally(problems):
    return counter([name for name, count in problems.items() for _ in range(count)])


def _window_measure(bucket):
    complete = [row for row in bucket if row['_complete']]
    return {
        'denominator': {
            'exported_orders': len(bucket),
            'complete_clock': len(complete),
            'excluded': len(bucket) - len(complete),
        },
        'problems': _tally(Counter(row['_problem'] for row in bucket if row['_problem'])),
        'to_preflight': delays([row['_to_preflight'] for row in complete], THRESHOLD_SECONDS),
        'to_entry': delays([row['_to_entry'] for row in complete], THRESHOLD_SECONDS),
    }


def frozen_orders(rows, cutoff):
    """Signal -> preflight -> native entry chain on accepted orders."""
    problems = Counter()
    complete, to_preflight, to_entry = [], [], []
    strata_rows, window_rows = [], []
    for row in rows:
        try:
            signal = inputs.utc(row['signal_at'])
            preflight = inputs.utc(row['preflight_at'])
            placed = inputs.utc(row['placed_at'])
        except (KeyError, TypeError, ValueError):
            problems['missing_timepoint'] += 1
            window_rows.append({'_complete': False, '_problem': 'missing_timepoint'})
            continue
        problem = None
        if signal > preflight:
            problem = 'signal_after_preflight'
        elif preflight > placed:
            problem = 'preflight_after_entry'
        elif placed >= cutoff:
            problem = 'entry_at_or_after_cutoff'
        if problem:
            problems[problem] += 1
            window_rows.append({'_complete': False, '_problem': problem, '_day': signal.date().isoformat()})
            continue
        to_preflight_value = _delay(signal, preflight)
        to_entry_value = _delay(preflight, placed)
        complete.append(row)
        to_preflight.append(to_preflight_value)
        to_entry.append(to_entry_value)
        window_rows.append({
            '_complete': True, '_problem': None, '_day': signal.date().isoformat(),
            '_to_preflight': to_preflight_value, '_to_entry': to_entry_value,
        })
        strata_rows.append({
            'sport': row.get('sport'),
            'market_kind': row.get('market_kind'),
            'to_preflight': to_preflight_value,
            'to_entry': to_entry_value,
        })

    def measure(bucket):
        return {
            'orders': len(bucket),
            'to_preflight': delays([item['to_preflight'] for item in bucket], THRESHOLD_SECONDS),
            'to_entry': delays([item['to_entry'] for item in bucket], THRESHOLD_SECONDS),
        }

    windows = defaultdict(list)
    for row in window_rows:
        windows[row.get('_day', 'unknown')].append(row)

    return {
        'denominator': {
            'exported_orders': len(rows),
            'complete_clock': len(complete),
            'excluded': len(rows) - len(complete),
        },
        'problems': _tally(problems),
        'to_preflight': delays(to_preflight, THRESHOLD_SECONDS),
        'to_entry': delays(to_entry, THRESHOLD_SECONDS),
        'windows': [
            {'day': day, 'value': _window_measure(bucket)}
            for day, bucket in sorted(windows.items())
        ],
        'strata': stratified(strata_rows, ('sport', 'market_kind'), measure),
    }


def minute_archive(frames, start=None, end=None, cutoff=None):
    """Fixed-interval frame boundary, duplicate and source-age coverage."""
    problems = Counter()
    slots, rows = [], []
    invalid_frames = 0
    for frame in frames:
        clock = inputs.frame_clock(frame)
        for name in clock['problems']:
            problems[name] += 1
        if 'slot' not in clock or clock['problems']:
            invalid_frames += 1
            continue
        slot, observed = clock['slot'], clock['observed']
        if start is not None and not start <= slot < end:
            problems['frame_outside_interval'] += 1
            continue
        slots.append(slot)
        for row in frame.get('rows') or []:
            if not row.get('signal_id'):
                problems['missing_signal_identifier'] += 1
                continue
            rows.append(row | {'_observed': observed, '_slot': slot})

    if start is None and slots:
        start = min(slots)
    if end is None and slots:
        end = max(slots) + ONE_MINUTE
    if start is None or end is None or not start < end:
        raise ValueError('minute archive requires a fixed start and end')
    expected = int((end - start) // ONE_MINUTE)
    distinct = {slot for slot in slots if start <= slot < end}
    if len(distinct) != len(set(slots)):
        problems['frame_outside_interval'] += len(set(slots)) - len(distinct)
    if len(slots) != len(set(slots)):
        problems['duplicate_slot'] += len(slots) - len(set(slots))

    kept, duplicate_rows = unique(rows, 'signal_id')
    source_age, quote_to_observation, to_checked, source_to_checked = [], [], [], []
    strata_rows = []
    usable = 0
    for row in kept:
        try:
            created = inputs.utc(row['created_at'])
            quoted = inputs.utc(row['quote_time'])
        except (KeyError, TypeError, ValueError):
            problems['invalid_row_timepoint'] += 1
            continue
        slot, observed = row['_slot'], row['_observed']
        begin, frame_end = slot - timedelta(seconds=90), slot - timedelta(seconds=30)
        if not begin <= created < frame_end:
            problems['created_outside_slot'] += 1
            continue
        if quoted > created:
            problems['quote_after_created'] += 1
            continue
        if quoted > observed:
            problems['quote_after_observation'] += 1
            continue
        checked = None
        checked_value = row.get('native_quote_checked_at')
        if checked_value:
            try:
                checked = inputs.utc(checked_value)
            except (TypeError, ValueError):
                problems['invalid_preflight_time'] += 1
            else:
                if checked < observed:
                    problems['preflight_before_observation'] += 1
                    checked = None
                elif checked - observed > timedelta(seconds=PAPER_PREFLIGHT_MAX_SECONDS):
                    problems['preflight_check_too_late'] += 1
                    checked = None
                elif cutoff is not None and checked > cutoff:
                    problems['preflight_after_cutoff'] += 1
                    checked = None
        source_age.append(_delay(quoted, created))
        quote_to_observation.append(_delay(quoted, observed))
        if checked is not None:
            to_checked.append(_delay(observed, checked))
            source_to_checked.append(_delay(quoted, checked))
        usable += 1
        strata_rows.append({
            'sport': row.get('sport'),
            'phase': row.get('phase'),
            'market_kind': row.get('market_kind'),
            'source_age': _delay(quoted, created),
            'to_checked': _delay(observed, checked) if checked else None,
        })

    def measure(bucket):
        return {
            'rows': len(bucket),
            'source_age': delays([item['source_age'] for item in bucket], THRESHOLD_SECONDS),
            'to_checked': delays([item['to_checked'] for item in bucket],
                                 Decimal(PAPER_PREFLIGHT_MAX_SECONDS)),
        }

    return {
        'interval': {'start': start, 'end': end},
        'denominator': {
            'frames': len(frames),
            'valid_frames': len(slots),
            'invalid_frames': invalid_frames,
            'distinct_slots': len(distinct),
            'expected_slots': expected,
            'missing_slots': expected - len(distinct),
            'capture_share': Decimal(len(distinct)) / expected if expected else None,
            'archived_rows': len(rows),
            'unique_rows': len(kept),
            'usable_rows': usable,
            'duplicate_rows': duplicate_rows,
        },
        'problems': _tally(problems),
        'statuses': counter([row.get('status') for row in kept]),
        'source_age': delays(source_age, THRESHOLD_SECONDS),
        'quote_to_observation': delays(quote_to_observation, THRESHOLD_SECONDS),
        'observed_to_preflight': delays(to_checked, Decimal(PAPER_PREFLIGHT_MAX_SECONDS)),
        'source_to_preflight': delays(source_to_checked, THRESHOLD_SECONDS),
        'strata': stratified(strata_rows, ('sport', 'phase', 'market_kind'), measure),
    }


def analyze(native_document, frames, start=None, end=None):
    begin, end_interval, cutoff = inputs.interval(native_document)
    orders = frozen_orders(inputs.rows(native_document), cutoff)
    archive = minute_archive(frames, start=start, end=end)
    return {
        'experiment': STUDY,
        'threshold_seconds': THRESHOLD_SECONDS,
        'paper_preflight_max_seconds': PAPER_PREFLIGHT_MAX_SECONDS,
        'interval': {'begin': begin, 'end': end_interval, 'cutoff': cutoff},
        'queues': {'frozen_orders': orders, 'minute_archive': archive},
        'cross_queue': {
            'common_identifier': None,
            'matched_opportunities': 0,
            'note': 'order and signal identifiers are disjoint; no record is reconstructed across queues',
        },
        'coverage': {
            'included': {
                'orders': orders['denominator']['complete_clock'],
                'archive_rows': archive['denominator']['usable_rows'],
            },
            'excluded': {
                'orders': orders['denominator']['excluded'],
                'duplicate_archive_rows': archive['denominator']['duplicate_rows'],
                'missing_slots': archive['denominator']['missing_slots'],
                'invalid_frames': archive['denominator']['invalid_frames'],
            },
            'unknown': {'signal_to_preflight_bridge': 'no common identifier'},
        },
    }
