"""Compare immutable natural-signal evidence with the registered minute sampler."""

import argparse
from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re

from execution_scope import NATIVE_SELECTION_CODES, native_outcome_codes, native_sport_id
from infra.profitability.experiments import inputs
from infra.profitability.experiments.stats import delays


QUOTE_AGE = timedelta(seconds=15)


def expected_options(row):
    sport = native_sport_id(row['sport'])
    return native_outcome_codes(row['market_kind'], sport) if sport else frozenset()


def complete_book(book, row, price):
    selected = NATIVE_SELECTION_CODES.get(row['selection'])
    expected = expected_options(row)
    parsed = {key: inputs.number(value) for key, value in book.items()}
    return bool(expected and parsed.keys() == expected and selected in parsed
                and parsed[selected] == inputs.number(price)
                and all(item is not None and item > 1 for item in parsed.values()))


def recommendation_book(row, signal_at):
    book = row['recommendation_book']
    if not book or not book.get('market_instance_id') or not book.get('play_id'):
        return 'snapshot_missing'
    try:
        received = inputs.utc(book['received_at'])
        quoted = inputs.utc(row['source_at'])
        options = book['options']
        prices = {NATIVE_SELECTION_CODES.get(option['selection']): option['price'] for option in options}
        option_ids = {option['selection_id'] for option in options}
    except (KeyError, TypeError, ValueError):
        return 'snapshot_invalid'
    if not quoted <= received <= signal_at:
        return 'snapshot_time_invalid'
    if (None in option_ids or '' in option_ids or len(option_ids) != len(options)
            or len(prices) != len(options) or not complete_book(prices, row, row['signal_price'])):
        return 'snapshot_invalid'
    return 'snapshot_fresh' if signal_at - quoted <= QUOTE_AGE else 'snapshot_aged'


def collect_minutes(directory, start, end, cutoff):
    records = {}
    slots = Counter()
    slot = start
    while slot < end:
        path = directory / (slot.isoformat().replace('+00:00', 'Z') + '.json')
        if not path.is_file():
            slots['missing'] += 1
        else:
            raw = json.loads(path.read_bytes())
            clock = inputs.frame_clock(raw)
            if clock['problems'] or clock.get('slot') != slot or clock['observed'] >= cutoff:
                slots['invalid'] += 1
            else:
                slots['valid'] += 1
                for row in raw['rows']:
                    key = row['signal_id']
                    created = inputs.utc(row['created_at'])
                    if not key or key in records or not clock['begin'] <= created < clock['end']:
                        raise ValueError('invalid or duplicate opportunity in paper archive')
                    records[key] = (row['status'], created, clock['observed'])
        slot += timedelta(minutes=1)
    return records, dict(slots)


def preflight_evidence(row, signal_at, cutoff):
    valid = []
    for attempt in row['preflights'] or ():
        try:
            captured = inputs.utc(attempt['captured_at'])
            checked = inputs.utc(attempt['checked_at'])
        except (TypeError, ValueError):
            continue
        if signal_at <= checked <= captured < cutoff and captured - checked <= QUOTE_AGE:
            valid.append((checked, attempt))
    return sorted(valid, key=lambda value: value[0])


def cost_artifacts(directory, fixtures):
    """Summarize references and hashed inputs, without authorizing a cost schedule."""
    digest = re.compile(r'[a-f0-9]{64}')
    available = set()
    invalid = 0
    for path in directory.iterdir():
        if not path.is_file() or not digest.fullmatch(path.name):
            continue
        raw = path.read_bytes()
        if raw and sha256(raw).hexdigest() == path.name:
            available.add(path.name)
        else:
            invalid += 1
    approvals = [approval for fixture in fixtures.values() for approval in fixture.get('approvals', [])]
    schedules = [approval.get('cost_schedule') for approval in approvals]
    return {'manifest_fixtures': len(fixtures), 'contract_approvals': len(approvals),
            'verified_digests': len(available), 'invalid_digests': invalid,
            'schedules_with_matching_digest': sum(isinstance(schedule, dict)
                and schedule.get('evidence_sha256') in available for schedule in schedules),
            'account_cost_certified_by_this_report': False}


def analyze(document, frames_path, manifest, costs_path, start, end):
    export_begin, export_end, cutoff = inputs.interval(document)
    if (document.get('schema') != 1 or not start < end or end > cutoff
            or start - timedelta(seconds=90) < export_begin or export_end < end - timedelta(seconds=90)
            or any(bound.second or bound.microsecond for bound in (start, end))):
        raise ValueError('event export does not cover the requested paper window')
    minute, slots = collect_minutes(frames_path, start, end, cutoff)
    fixtures = manifest['fixtures']
    counts = Counter()
    phase_coverage = defaultdict(Counter)
    prior_status = Counter()
    attempts = Counter()
    before_minute = []
    minute_lag = []
    seen = set()
    for row in inputs.rows(document):
        key = row['signal_id']
        signal_at = inputs.utc(row['signal_at'])
        if not key or key in seen or not export_begin <= signal_at < export_end:
            raise ValueError('invalid or duplicate event identity')
        seen.add(key)
        sampled = minute.get(key)
        if not sampled or sampled[1] != signal_at:
            counts['event_without_same_minute_signal'] += 1
            continue
        counts['paired_signals'] += 1
        phase = phase_coverage[row['phase']]
        phase['paired'] += 1
        status, _, observed = sampled
        minute_lag.append(Decimal(str((observed - signal_at).total_seconds())))
        snapshot = recommendation_book(row, signal_at)
        counts[snapshot] += 1
        phase[snapshot] += 1
        valid = preflight_evidence(row, signal_at, cutoff)
        if not valid:
            counts['without_verified_preflight_clock'] += 1
            continue
        counts['with_verified_preflight_clock'] += 1
        if row['fixture_id'] in fixtures:
            counts['preflight_signals_with_manifest_fixture'] += 1
        for checked, attempt in valid:
            attempts['clock_valid'] += 1
            attempts['native_accepted'] += int(attempt['native_accepted'] is True)
            attempts['reference_contract_missing'] += int(attempt['reference_contract_missing'] is True)
            decision = attempt['price_decision']
            attempts['runtime_price_pass'] += int(decision == 'pass')
            attempts['runtime_price_ineligible'] += int(decision == 'reference_ineligible')
        earliest, _ = valid[0]
        if earliest < observed:
            counts['preflight_before_minute_observation'] += 1
            prior_status[status] += 1
            before_minute.append(Decimal(str((observed - earliest).total_seconds())))
        if any(checked < observed and complete_book(attempt['native_book'] or {}, row, attempt['price'])
               for checked, attempt in valid):
            counts['complete_preflight_book_before_minute'] += 1
    return {'scope': 'historical_event_coverage', 'window': {'start': start, 'end': end, 'cutoff': cutoff},
            'slots': slots, 'minute_signals': len(minute), 'event_signals': len(seen),
            'minute_signals_missing_in_export': len(minute.keys() - seen),
            'coverage': dict(counts), 'by_phase': dict(phase_coverage), 'attempts': dict(attempts),
            'minute_status_after_earlier_preflight': dict(prior_status),
            'signal_to_minute_seconds': delays(minute_lag, Decimal(30)),
            'earlier_preflight_to_minute_seconds': delays(before_minute, Decimal(30)),
            'reference_data': cost_artifacts(costs_path, fixtures),
            'limits': ['a recommendation snapshot is not proof of an executable quote',
                       'earlier preflights come from the original dispatch population',
                       'a reference digest does not establish account cost or net profitability']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('export', type=Path)
    parser.add_argument('frames', type=Path)
    parser.add_argument('reference', type=Path)
    parser.add_argument('costs', type=Path)
    parser.add_argument('start', type=inputs.utc)
    parser.add_argument('end', type=inputs.utc)
    args = parser.parse_args()
    data = args.export.read_bytes()
    result = analyze(json.loads(data), args.frames, json.loads(args.reference.read_bytes()),
                     args.costs, args.start, args.end)
    print(json.dumps(result | {'event_export_sha256': sha256(data).hexdigest()}, default=str, sort_keys=True))


if __name__ == '__main__':
    main()
