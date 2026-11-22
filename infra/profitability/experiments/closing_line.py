"""P02: strict same-contract closing-line value (CLV) audit.

A pair is only priced when the fixture mapping, period, market kind, line, direction
and score basis all match, and when the entry quote was observed strictly before
kickoff. The closing snapshot is a post-hoc reference only: it is never treated as an
earlier entry price, and each closing book is reported separately instead of blended.

CLV is expressed as the executable entry price relative to the reference closing
price, ``entry / closing - 1``, plus the implied-probability gain ``1/entry -
1/closing``. Money and odds stay ``Decimal``; only bootstrap and summary statistics
convert to ``float``.
"""

import random
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from .evidence import (
    INSUFFICIENT, decimal, load_json, quantized, section,
)
from .public_results import (
    CLOSING_BOOKS, closing_coverage, load_directory,
)

ONE = Decimal(1)
BOOTSTRAP_ROUNDS = 2000
BOOTSTRAP_SEED = 20261122


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f'not a number: {value!r}') from error


def _line(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return decimal(value, name='line').normalize()


def pair_clv(entry, closing):
    """Compare one entry quote with one closing snapshot of the same contract.

    ``entry`` requires ``fixture, period, kind, line, direction, score_basis, price,
    observed_at, kickoff`` and ``closing`` requires the same identity fields plus
    ``prices`` (direction -> Decimal) and ``book``. Both ``fixture`` values must be
    the verified public mapping; ``None`` never matches.
    """
    reasons = []
    if not entry.get('fixture') or entry.get('fixture') != closing.get('fixture'):
        reasons.append('fixture_mapping_missing')
    for field in ('period', 'kind', 'score_basis', 'direction'):
        if entry.get(field) != closing.get(field):
            reasons.append(f'{field}_mismatch')
    if _line(entry.get('line')) != _line(closing.get('line')):
        reasons.append('line_mismatch')
    if entry.get('kickoff') is None or closing.get('kickoff') is None:
        reasons.append('kickoff_time_missing')
    elif entry['kickoff'] != closing['kickoff']:
        reasons.append('kickoff_time_mismatch')
    elif not entry['observed_at'] < entry['kickoff']:
        reasons.append('entry_not_before_kickoff')

    direction = closing.get('direction')
    closing_prices = closing.get('prices') or {}
    closing_price = closing_prices.get(direction) if direction else None
    if closing_price is None:
        reasons.append('closing_side_missing')
    elif (not isinstance(closing_price, Decimal) or not closing_price.is_finite()
          or closing_price <= ONE):
        reasons.append('closing_price_invalid')
        closing_price = None

    entry_price = None
    try:
        entry_price = decimal(entry.get('price'), name='entry price')
    except ValueError:
        reasons.append('entry_price_invalid')
    if entry_price is not None and entry_price <= ONE:
        reasons.append('entry_price_invalid')
        entry_price = None

    if reasons or entry_price is None or closing_price is None:
        return {'status': 'rejected', 'reasons': sorted(set(reasons))}

    price_clv = quantized(entry_price / closing_price - ONE)
    implied_gain = quantized(ONE / entry_price - ONE / closing_price)
    return {
        'status': 'paired',
        'book': closing['book'],
        'price_clv': price_clv,
        'implied_probability_gain': implied_gain,
        'beats_closing': price_clv > 0,
    }


def _percentile(values, share):
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * share
    try:
        lower = int(position)
    except (TypeError, ValueError) as error:
        raise ValueError(f'invalid percentile position {position!r}') from error
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def summarize(pairs, *, total_observations, seed=BOOTSTRAP_SEED):
    """Fixture-level coverage and interval for one closing book."""
    by_fixture = defaultdict(list)
    rejected = Counter()
    for pair in pairs:
        if pair['status'] == 'paired':
            by_fixture[pair['fixture']].append(pair['price_clv'])
        else:
            rejected.update(pair['reasons'])
    fixture_means = [sum(values, Decimal(0)) / len(values) for values in by_fixture.values()]
    if not fixture_means:
        return {
            'paired_observations': 0, 'paired_fixtures': 0,
            'observations': total_observations, 'coverage': 0.0,
            'rejection_reasons': dict(rejected),
        }
    points = [_float(value) for value in fixture_means]
    rng = random.Random(seed)
    draws = []
    for _ in range(BOOTSTRAP_ROUNDS):
        sample = [points[rng.randrange(len(points))] for _ in points]
        draws.append(sum(sample) / len(sample))
    all_pairs = [pair for pair in pairs if pair['status'] == 'paired']
    better = sum(1 for pair in all_pairs if pair['beats_closing'])
    return {
        'observations': total_observations,
        'paired_observations': len(all_pairs),
        'paired_fixtures': len(by_fixture),
        'coverage': round(len(all_pairs) / total_observations, 6) if total_observations else 0.0,
        'mean_price_clv': quantized(Decimal(str(sum(points) / len(points)))),
        'median_price_clv': quantized(Decimal(str(_percentile(points, 0.5)))),
        'share_beating_closing': round(better / len(all_pairs), 6),
        'interval95_price_clv': [quantized(Decimal(str(_percentile(draws, 0.025)))),
                                 quantized(Decimal(str(_percentile(draws, 0.975))))],
        'rejection_reasons': dict(rejected),
    }


def _cohort_observations(cohort_dir):
    """Entry quotes present in the frozen cohorts, with any verifiable identity."""
    sources, observations = [], []
    for name in ('native-cohort.json', 'market-adjusted-cohort.json'):
        path = Path(cohort_dir) / name
        if not path.is_file():
            continue
        document, record = load_json(path, cohort_dir)
        sources.append(record)
        for row in document.get('rows', ()):
            if name == 'native-cohort.json':
                if row.get('preflight_price') and row.get('preflight_at'):
                    observations.append({
                        'fixture': None, 'price': row['preflight_price'],
                        'observed_at': row['preflight_at'], 'origin': 'native_preflight',
                    })
                price, at = row.get('signal_price'), row.get('signal_at')
            else:
                price, at = row.get('recommendation_price'), row.get('created_at')
            observations.append({
                'fixture': None, 'price': price, 'observed_at': at,
                'origin': name.removesuffix('.json'),
                'market_kind': row.get('market_kind'), 'period': row.get('period'),
                'selection': row.get('selection'),
            })
    return sources, observations


def run(cohort_dir, public_dir, root=None):
    """Read the frozen cohorts and public closing files; price only mapped pairs."""
    sources, observations = _cohort_observations(cohort_dir)
    coverage = {'entry_observations': len(observations), 'mapped_observations': 0,
                'paired_observations': 0, 'paired_fixtures': 0}
    missing = Counter()
    readiness = {}
    try:
        public = load_directory(public_dir, root)
    except FileNotFoundError as error:
        missing[f'public_csv_unavailable: {error}'] += 1
        public = None
    if public is not None:
        sources.extend(public['provenance'])
        readiness = closing_coverage(public['matches'])
        missing.update({f'csv_skipped_{key}': value for key, value in public['skipped'].items()})
        # A public row carries club names and a local kickoff clock; the frozen
        # cohorts identify fixtures by provider id and carry no club names, so no
        # observation can be matched without inventing a mapping.
        missing['no_team_identity_in_frozen_cohorts'] += 1
        missing['closing_snapshot_is_reference_not_entry_price'] += 1
    books = {book: summarize([], total_observations=len(observations)) for book in CLOSING_BOOKS}
    return section(
        label=INSUFFICIENT,
        sources=sources,
        missing=missing,
        coverage=coverage,
        results={
            'books': books,
            'closing_reference_readiness': readiness,
            'clv_definition': 'entry/closing - 1 on the identical contract, per closing book',
        },
        limitations=[
            'public closing odds have no verified snapshot timestamp or timezone',
            'closing prices were not executable and cannot justify an earlier entry',
        ],
    )
