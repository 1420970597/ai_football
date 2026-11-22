"""Shared tabulation, delay and coverage primitives for the experiments."""

from collections import Counter, defaultdict
from decimal import ROUND_FLOOR, Decimal

ZERO = Decimal(0)


def counter(values):
    return dict(sorted(Counter(values).items(), key=lambda item: str(item[0])))


def unique(rows, key):
    """Split rows into first occurrences and duplicates by a stable key."""
    seen, kept, duplicates = set(), [], 0
    for row in rows:
        value = row.get(key)
        if value in seen:
            duplicates += 1
            continue
        seen.add(value)
        kept.append(row)
    return kept, duplicates


def quantile(values, fraction):
    """Linear-interpolated quantile over Decimals; None for an empty series."""
    ordered = sorted(value for value in values if value is not None)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * Decimal(str(fraction))
    try:
        lower = int(position.to_integral_value(rounding=ROUND_FLOOR))
    except (TypeError, ValueError) as error:
        raise ValueError('invalid quantile position') from error
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def delays(values, threshold):
    """Delay series with an explicit 30-second-style share and quantiles."""
    present = [value for value in values if value is not None]
    within = sum(value <= threshold for value in present)
    return {
        'observations': len(present),
        'within_threshold': within,
        'threshold_seconds': threshold,
        'within_share': Decimal(within) / len(present) if present else None,
        'p50': quantile(present, Decimal('0.5')),
        'p90': quantile(present, Decimal('0.9')),
        'p99': quantile(present, Decimal('0.99')),
        'maximum': max(present) if present else None,
    }


def seconds(start, stop):
    return (stop - start).total_seconds()


def stratified(rows, dimensions, measure):
    """Group by ``dimensions`` and apply ``measure`` to each bucket."""
    groups = defaultdict(list)
    for row in rows:
        key = tuple(row.get(name) for name in dimensions)
        groups[key].append(row)
    return [
        {'dimensions': dict(zip(dimensions, key, strict=True)), 'value': measure(bucket)}
        for key, bucket in sorted(groups.items(), key=lambda item: tuple(str(part) for part in item[0]))
    ]


def cross_tab(rows, dimensions, reason):
    """Reason x dimension counts plus the denominator of every group."""
    counts = Counter()
    totals = Counter()
    for row in rows:
        key = tuple(row.get(name) for name in dimensions)
        value = row.get(reason)
        counts[key + (value,)] += 1
        totals[key] += 1
    cells = [
        {'dimensions': dict(zip(dimensions, key[:-1], strict=True)), 'value': key[-1], 'count': count,
         'group_denominator': totals[key[:-1]]}
        for key, count in sorted(counts.items(), key=lambda item: tuple(str(part) for part in item[0]))
    ]
    return {'cells': cells, 'group_denominators': [
        {'dimensions': dict(zip(dimensions, key, strict=True)), 'count': count}
        for key, count in sorted(totals.items(), key=lambda item: tuple(str(part) for part in item[0]))
    ], 'total': len(rows)}


def mean(values):
    ordered = list(values)
    return sum(ordered, ZERO) / len(ordered) if ordered else None


def sample_variance(values):
    ordered = list(values)
    if len(ordered) < 2:
        return None
    average = mean(ordered)
    return sum(((value - average) ** 2 for value in ordered), ZERO) / (len(ordered) - 1)


def design_effect(cluster_variance, unit_variance):
    """Variance inflation of cluster resampling against independent units."""
    if not unit_variance or cluster_variance is None or unit_variance <= 0:
        return None
    return cluster_variance / unit_variance


def required_two_point_sd(sd, margin=Decimal('0.02'), z=Decimal('1.959963984540054')):
    """Independent-sample size for a two-sided 95% interval of ``margin``."""
    if sd is None or sd <= 0:
        return None
    return (z * sd / margin) ** 2


def interval(below, above):
    return {'low': below, 'high': above}


def two_sided_interval(values, low_fraction=Decimal('0.025'), high_fraction=Decimal('0.975')):
    return interval(quantile(values, low_fraction), quantile(values, high_fraction))
