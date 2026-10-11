"""As-of numeric features. Unknown event codes and absent history stay missing."""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

FEATURE_VERSION = 'market-offset-windows-v3'
BASE_FEATURE_COUNT = 30
WINDOW_SECONDS = (15, 60, 180)
HISTORY_MAX_POINTS = 256
PEERS = ('poisson_market', 'poisson_time_decay', 'devig_consensus',
         'economics_risk_adjusted', 'microstructure_adjusted')
EXTRA_FEATURES = tuple(f'window_{seconds}s_{name}' for seconds in WINDOW_SECONDS
                       for name in ('change', 'velocity', 'volatility', 'coverage', 'missing')) + (
    'history_duration', 'quote_age', 'quote_age_missing', 'overround', 'overround_missing',
    'red_card_difference', 'red_card_missing', 'score_time_interaction',
    'line_score_interaction', 'red_card_time_interaction',
    *[f'{peer}_missing' for peer in PEERS])
EXTRA_INDEX = {name: BASE_FEATURE_COUNT + i for i, name in enumerate(EXTRA_FEATURES)}


def logit(probability: float) -> float:
    if not math.isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError('无效概率')
    p = min(1 - 1e-6, max(1e-6, probability))
    return math.log(p / (1 - p))


def visible_history(row: Mapping[str, Any]) -> list[list[float]]:
    cutoff = row.get('input_cutoff_ms')
    points: dict[float, list[float]] = {}
    for raw in row.get('training_history') or []:
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            continue
        try:
            values = [float(v) for v in raw]
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in values) or values[1] <= 1:
            continue
        if cutoff is not None and (values[0] > cutoff or len(values) >= 3 and values[2] > cutoff):
            continue
        points[values[0]] = values
    return [points[t] for t in sorted(points)]


def extra_features(row: Mapping[str, Any], base: Sequence[float]) -> list[float]:
    history = visible_history(row)
    # Fair probabilities require a complete two/three-way quote at that historical
    # Raw legacy points contribute a movement signal only. Four-field points
    # are accepted as normalized probabilities from a trusted projection.
    signal_points = []
    for point in history:
        if len(point) >= 4 and 0 < point[3] < 1:
            signal_points.append((point[0], logit(point[3])))
        else:
            signal_points.append((point[0], -math.log(max(1.000001, point[1]))))
    now = float(row.get('input_cutoff_ms') or (history[-1][0] if history else 0))
    result: list[float] = []
    for seconds in WINDOW_SECONDS:
        inside = [p for p in signal_points if now - seconds * 1000 <= p[0] <= now]
        before = [p for p in signal_points if p[0] < now - seconds * 1000]
        if before and inside:
            inside.insert(0, before[-1])
        if len(inside) < 2 or now - inside[-1][0] > seconds * 1000:
            result.extend((0, 0, 0, 0, 1))
            continue
        span = (inside[-1][0] - inside[0][0]) / 1000
        if span <= 0:
            result.extend((0, 0, 0, 0, 1))
            continue
        change = inside[-1][1] - inside[0][1]
        changes = [b[1] - a[1] for a, b in zip(inside, inside[1:])]
        volatility = math.sqrt(sum(v * v for v in changes) / span)
        result.extend((max(-2, min(2, change)) / 2,
                       max(-1, min(1, change * 10 / span)),
                       min(1, volatility), min(1, span / seconds), 0))
    duration = (history[-1][0] - history[0][0]) / 1000 if len(history) > 1 else 0
    context = row.get('feature_context') or {}
    age, margin, red = context.get('quote_age_s'), context.get('overround'), context.get('red_card_difference')
    for value in (age, margin, red):
        if value is not None and not math.isfinite(float(value)):
            raise ValueError('无效盘口或赛况特征')
    red_value = max(-3, min(3, float(red))) / 3 if red is not None else 0
    result.extend((min(1, duration / 180), min(1, max(0, float(age)) / 300) if age is not None else 0,
                   float(age is None), max(-1, min(1, float(margin))) if margin is not None else 0,
                   float(margin is None), red_value, float(red is None),
                   base[4] * (1 - base[3]), base[13] * base[4], red_value * (1 - base[3])))
    peers = row.get('algorithm_probabilities') or {}
    result.extend(float(peers.get(peer) is None) for peer in PEERS)
    return result


def upgrade_vector(values: Sequence[float]) -> list[float]:
    x = [float(v) for v in values]
    if not all(math.isfinite(v) for v in x):
        raise ValueError('无效特征向量')
    if len(x) == BASE_FEATURE_COUNT:
        extra = [0.0] * len(EXTRA_FEATURES)
        for name in EXTRA_FEATURES:
            if name.endswith('_missing'):
                extra[EXTRA_INDEX[name] - BASE_FEATURE_COUNT] = 1
        # Interactions are exactly recoverable from certified legacy fields.
        extra[EXTRA_INDEX['score_time_interaction'] - BASE_FEATURE_COUNT] = x[4] * (1 - x[3])
        extra[EXTRA_INDEX['line_score_interaction'] - BASE_FEATURE_COUNT] = x[13] * x[4]
        # Legacy aggregate missingness cannot identify individual absent peers.
        for i, peer in enumerate(PEERS):
            extra[EXTRA_INDEX[f'{peer}_missing'] - BASE_FEATURE_COUNT] = float(x[29] > 0 and x[24 + i] == 0)
        x.extend(extra)
    if len(x) != BASE_FEATURE_COUNT + len(EXTRA_FEATURES):
        raise ValueError('模型特征版本不匹配')
    return x


def residual_vector(values: Sequence[float]) -> list[float]:
    x = upgrade_vector(values)
    prior = x[1]
    x[1] = 0.0  # market coefficient is an offset, never penalized towards 0.5
    x[2] -= prior
    for i, peer in enumerate(PEERS):
        x[24 + i] = 0.0 if x[EXTRA_INDEX[f'{peer}_missing']] else x[24 + i] - prior
    return x


def verified_context(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    # Only explicit normalized fields are accepted. Provider numeric codes are
    # deliberately not interpreted as red/yellow cards without verified mapping.
    context = dict(snapshot.get('feature_context') or {})
    if 'red_card_difference' not in context:
        cards = snapshot.get('red_cards')
        if isinstance(cards, (list, tuple)) and len(cards) == 2 and all(
                isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 5 for v in cards):
            context['red_card_difference'] = cards[0] - cards[1]
    return context
