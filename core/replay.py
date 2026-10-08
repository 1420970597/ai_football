"""Proper probability scores and independent-selection uncertainty."""
from __future__ import annotations

import math
from typing import Sequence, Tuple

CONFIDENCE_Z = 1.959963984540054


def wilson(successes: int, n: int) -> Tuple[float, float]:
    if n <= 0 or successes < 0 or successes > n:
        raise ValueError('invalid independent sample counts')
    p = successes / n
    z2 = CONFIDENCE_Z ** 2
    center = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = CONFIDENCE_Z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return max(0, center - half), min(1, center + half)


def proper_scores(probabilities: Sequence[float], actual_index: int) -> Tuple[float, float]:
    if not probabilities or not 0 <= actual_index < len(probabilities):
        raise ValueError('invalid outcome')
    if any(not math.isfinite(p) or p < 0 or p > 1 for p in probabilities):
        raise ValueError('invalid probabilities')
    if abs(sum(probabilities) - 1) > .001:
        raise ValueError('probabilities must sum to one')
    brier = sum((p - int(i == actual_index)) ** 2 for i, p in enumerate(probabilities))
    log_loss = -math.log(max(1e-12, probabilities[actual_index]))
    return brier, log_loss
