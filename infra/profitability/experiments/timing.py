"""P08: fixed lead-time and pre-kickoff market-movement stratification.

Recommendations are bucketed by the fixed lead time between the kickoff instant and
the recommendation, then described by the same-contract quote history that was
actually visible at the recommendation. A quote is visible only when both its native
``quote_time`` and its ``received_at`` are at or before the recommendation and it was
taken before kickoff; a closing snapshot is never backfilled as history.

When the kickoff, the contract mapping or a pre-recommendation quote is missing, the
record keeps the named reason and contributes an independent diagnostic built from the
entry interval that was genuinely visible, instead of being dropped silently.
"""

from collections import Counter, defaultdict
from datetime import timedelta
import json
from math import isnan
from pathlib import Path

from .evidence import INSUFFICIENT, MEASURED, load_json, section, utc
from .market import _engine

BUCKETS = (
    ('le_5m', 0, 5),
    ('5m_30m', 5, 30),
    ('30m_2h', 30, 120),
    ('2h_6h', 120, 360),
    ('6h_24h', 360, 1440),
    ('gt_24h', 1440, None),
)


def lead_bucket(recommendation_time, kickoff):
    """Fixed lead-time bucket, or ``live`` when the recommendation is post-kickoff."""
    if kickoff is None:
        return None
    minutes = (kickoff - recommendation_time).total_seconds() / 60
    if minutes < 0:
        return 'live'
    for label, low, high in BUCKETS:
        if minutes >= low and (high is None or minutes < high):
            return label
    return None


def _mid(quote):
    try:
        bid = float(quote['bid'])
        ask = float(quote['ask'])
    except (KeyError, TypeError, ValueError):
        return None
    value = (bid + ask) / 2
    return None if isnan(value) else value


def visible_movement(quotes, recommendation_time, kickoff):
    """Same-contract movement from quotes visible at ``recommendation_time``.

    ``quotes`` carry ``quote_time`` and ``received_at``. Both must be at or before
    the recommendation, and ``quote_time`` must be before kickoff. Post-kickoff or
    later-arriving quotes are counted as unavailable, never used to backfill.
    """
    visible = []
    unseen = 0
    post_kickoff = 0
    for quote in quotes:
        try:
            native = utc(quote['quote_time'])
            received = utc(quote['received_at'])
        except (KeyError, TypeError, ValueError):
            unseen += 1
            continue
        if received > recommendation_time or native > recommendation_time:
            unseen += 1
            continue
        if kickoff is not None and native >= kickoff:
            post_kickoff += 1
            continue
        value = _mid(quote)
        if value is not None:
            visible.append((native, value))
    missing = []
    if unseen:
        missing.append('quote_not_visible_before_recommendation')
    if post_kickoff:
        missing.append('only_post_kickoff_quote')
    if not visible:
        missing.append('no_pre_recommendation_quote')
        return None, missing
    visible.sort()
    return {
        'first': visible[0][1], 'last': visible[-1][1],
        'delta': visible[-1][1] - visible[0][1],
        'observations': len(visible),
    }, missing


def load_book_index(books_dir):
    """Index captured book snapshots by market slug, keeping native and seen times."""
    index = defaultdict(lambda: {'scheduled_start': None, 'market_type': None, 'line': None, 'quotes': []})
    for path in sorted(books_dir.glob('*.jsonl')):
        for line in path.read_bytes().splitlines():
            if not line.strip():
                continue
            document = json.loads(line)
            for market in document.get('markets', ()):
                slug = market.get('market')
                if not slug:
                    continue
                entry = index[slug]
                if entry['scheduled_start'] is None and market.get('scheduled_start'):
                    entry['scheduled_start'] = utc(market['scheduled_start'])
                entry['market_type'] = market.get('market_type')
                entry['line'] = market.get('line')
                entry['quotes'].extend(market.get('books', ()))
    return index


def load_bindings(reference_document):
    """fixture_id -> list of verified market bindings."""
    bindings = defaultdict(list)
    for fixture_id, fixture in reference_document.get('fixtures', {}).items():
        for binding in fixture.get('bindings', ()):
            bindings[fixture_id].append(binding)
    return bindings


def match_binding(bindings, pick):
    """Choose the binding whose market covers the recommendation's contract/side."""
    selection = str(pick.get('selection') or '')
    kind = pick.get('market_kind')
    period = pick.get('period')
    line = pick.get('line')
    candidates = [binding for binding in bindings
                  if binding.get('kind') == kind and binding.get('period') == period
                  and (line is None or binding.get('line') is None or str(binding['line']) == str(line))]
    if not candidates:
        return None
    for binding in candidates:
        if selection and selection in str(binding.get('market')):
            return binding
    return candidates[0] if len(candidates) == 1 else None


def analyze(picks, bindings, book_index, cutoff, *, result_at):
    """Bucket prepared picks by fixed lead time and attach visible movement."""
    buckets = defaultdict(list)
    missing = Counter()
    diagnostics = []
    for pick in picks:
        created = pick['created_at']
        result = result_at(pick, cutoff)
        if result is None:
            missing['settlement_unavailable'] += 1
            continue
        won, stake = result
        pick['_net'] = float(won * pick['recommendation_price'] - stake)
        binding = match_binding(bindings.get(pick['fixture_id'], ()), pick)
        if binding is None:
            missing['contract_mapping_missing'] += 1
            buckets['unmapped'].append(pick)
            continue
        entry = book_index.get(binding.get('market'))
        if entry is None:
            missing['book_history_missing'] += 1
            buckets['no_history'].append(pick)
            continue
        kickoff = entry['scheduled_start']
        bucket = lead_bucket(created, kickoff)
        if bucket is None:
            missing['kickoff_time_missing'] += 1
            bucket = 'no_kickoff'
        movement, movement_missing = visible_movement(entry['quotes'], created, kickoff)
        missing.update(movement_missing)
        pick['_movement'] = movement
        buckets[bucket].append(pick)
        if movement is None:
            last_visible = max((utc(quote['received_at']) for quote in entry['quotes']
                                if quote.get('received_at') and utc(quote['received_at']) <= created),
                               default=None)
            if last_visible:
                diagnostics.append((created - last_visible).total_seconds())

    def describe(rows):
        settled = [row for row in rows if '_net' in row]
        moved = [row for row in rows if row.get('_movement')]
        return {
            'recommendations': len(rows),
            'settled': len(settled),
            'with_visible_history': len(moved),
            'mean_movement': (round(sum(row['_movement']['delta'] for row in moved) / len(moved), 6)
                              if moved else None),
            'mean_receipt_net': (round(sum(row['_net'] for row in settled) / len(settled), 6)
                                 if settled else None),
        }

    return {
        'buckets': {bucket: describe(rows) for bucket, rows in sorted(buckets.items())},
        'missing': dict(missing),
        'ungrouped_diagnostics': {'count': len(diagnostics),
                                  'mean_seconds': sum(diagnostics) / len(diagnostics) if diagnostics else None},
    }


def run(cohort_path, reference_path, books_dir):
    """P08 entry: fixed lead-time stratification with visible pre-kickoff history."""
    prepare, result_at = _engine()
    cohort, cohort_record = load_json(cohort_path, Path(cohort_path).parent)
    reference, reference_record = load_json(reference_path, Path(reference_path).parent)
    cutoff = utc(cohort['cutoff'])
    picks = []
    missing = Counter()
    for row in cohort.get('rows', ()):
        status, pick = prepare(row)
        if status != 'verified':
            missing[f'prepare_{status}'] += 1
            continue
        picks.append(pick)
    book_index = load_book_index(Path(books_dir))
    result = analyze(picks, load_bindings(reference), book_index, cutoff, result_at=result_at)
    missing.update(result['missing'])
    covered = sum(bucket['with_visible_history'] for bucket in result['buckets'].values())
    return section(
        label=MEASURED if covered else INSUFFICIENT,
        sources=[cohort_record, reference_record],
        missing=missing,
        coverage={'verified_picks': len(picks), 'with_visible_history': covered},
        results=result,
        limitations=[
            'closing odds are never used as pre-recommendation history',
            'a recommendation with no visible same-contract quote keeps a named gap',
        ],
    )


if __name__ == '__main__':  # boundary self-check, no archives needed
    from datetime import datetime, timezone
    kickoff = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    assert lead_bucket(kickoff - timedelta(minutes=3), kickoff) == 'le_5m'
    assert lead_bucket(kickoff - timedelta(hours=3), kickoff) == '2h_6h'
    assert lead_bucket(kickoff + timedelta(minutes=1), kickoff) == 'live'
    quotes = [
        {'quote_time': '2026-11-20T10:00:00+00:00', 'received_at': '2026-11-20T10:00:01+00:00', 'bid': '0.4', 'ask': '0.42'},
        {'quote_time': '2026-11-20T11:00:00+00:00', 'received_at': '2026-11-20T11:30:00+00:00', 'bid': '0.5', 'ask': '0.52'},
    ]
    movement, missing = visible_movement(quotes, datetime(2026, 11, 20, 11, 0, tzinfo=timezone.utc), kickoff)
    assert movement is not None and movement['observations'] == 1, movement
    assert 'quote_not_visible_before_recommendation' in missing
    print('timing self-check ok', movement)
