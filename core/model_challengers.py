"""Small, dependency-free P2 challenger primitives for synthetic/offline work.

These candidates are deliberately not wired into the model worker. They require
an explicit two-time training contract, so legacy projections cannot be fit by
accident. The stump is a one-split market-offset logistic challenger; the
dynamic Poisson helper is an event-visibility/rate sensitivity prototype, not a
production-calibrated model.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence, cast

from core.live_model import remaining_distribution
from core.model_validation import is_graded_row

MIN_LEAF_MATCHES = 2
MAX_CANDIDATE_THRESHOLDS = 64
LEAF_L2 = 1.0
MAX_LOGIT_DELTA = 2.0
RED_CARD_RATE_EFFECT = 0.12
REGULATION_SECONDS = 90 * 60


def _instant(value: Any) -> float:
    from datetime import datetime
    if not isinstance(value, str):
        raise ValueError('时间契约必须是带时区 ISO 字符串')
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError('时间契约必须包含时区')
    return dt.timestamp()


def validate_training_rows(rows: Sequence[Mapping[str, Any]]) -> None:
    """Require evidence clocks before any challenger is allowed to fit."""
    if not rows:
        raise ValueError('训练样本为空')
    for row in rows:
        cutoff = row.get('cutoff_ms')
        received = row.get('received_at_ms')
        model_ready = row.get('model_available_at_ms')
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in
               (cutoff, received, model_ready)):
            raise ValueError('缺少 cutoff/received/model_available 双时间契约')
        cutoff, received, model_ready = (float(cast(float, cutoff)), float(cast(float, received)),
                                         float(cast(float, model_ready)))
        if min(cutoff, received, model_ready) <= 0 or received > cutoff or model_ready > cutoff:
            raise ValueError('训练输入在决策截点之后才可见')
        cutoff_at = _instant(row.get('at'))
        label_at = _instant(row.get('label_available_at'))
        if label_at <= cutoff_at:
            raise ValueError('标签必须在决策截点之后才可用')
        if row.get('match_id') in (None, ''):
            raise ValueError('缺少比赛身份')
        is_graded_row(row)


def _logit(p: float) -> float:
    if not math.isfinite(p) or not 0 < p < 1:
        raise ValueError('市场基线概率必须在(0,1)')
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-max(-40.0, min(40.0, x))))


def _leaf_delta(rows: Sequence[Mapping[str, Any]], indices: Sequence[int]) -> float:
    # Newton solve for a regularized constant residual on top of market logit.
    delta = 0.0
    for _ in range(40):
        gradient = LEAF_L2 * delta
        hessian = LEAF_L2
        for i in indices:
            row = rows[i]
            p = _sigmoid(_logit(float(row['baseline'])) + delta)
            weight = float(row.get('observation_weight', 1.0))
            if not math.isfinite(weight) or not 0 < weight <= 1:
                raise ValueError('样本权重无效')
            gradient += weight * (p - int(row['y']))
            hessian += weight * p * (1 - p)
        step = gradient / hessian
        delta = max(-MAX_LOGIT_DELTA, min(MAX_LOGIT_DELTA, delta - step))
        if abs(step) < 1e-8:
            break
    return delta


def _loss(rows: Sequence[Mapping[str, Any]], indices: Sequence[int], delta: float) -> float:
    total = 0.0
    for i in indices:
        row = rows[i]
        p = min(1 - 1e-12, max(1e-12, _sigmoid(_logit(float(row['baseline'])) + delta)))
        y = int(row['y'])
        total -= float(row.get('observation_weight', 1.0)) * (y * math.log(p) + (1-y) * math.log1p(-p))
    return total + LEAF_L2 * delta * delta / 2


def fit_market_stump(rows: Sequence[Mapping[str, Any]], *, feature: str) -> dict[str, Any]:
    """Fit one depth-1 split; rows must be an already isolated training fold."""
    validate_training_rows(rows)
    rows = [row for row in rows if is_graded_row(row)]
    if not rows:
        raise ValueError('训练上下文没有可计二元损失的样本')
    if not feature:
        raise ValueError('必须指定单一挑战特征')
    values = []
    for i, row in enumerate(rows):
        try:
            value = float(row['features'][feature])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('挑战特征缺失或无效') from exc
        if not math.isfinite(value):
            raise ValueError('挑战特征非有限值')
        values.append((value, i))
    unique = sorted({v for v, _ in values})
    thresholds = [(a + b) / 2 for a, b in zip(unique, unique[1:])]
    if len(thresholds) > MAX_CANDIDATE_THRESHOLDS:
        # Deterministic quantile-like thinning; never depends on outcomes.
        thresholds = [thresholds[i * (len(thresholds)-1) // (MAX_CANDIDATE_THRESHOLDS-1)]
                      for i in range(MAX_CANDIDATE_THRESHOLDS)]
    best: tuple[float, float, float, float] | None = None
    for threshold in thresholds:
        left = [i for value, i in values if value <= threshold]
        right = [i for value, i in values if value > threshold]
        if (len({str(rows[i]['match_id']) for i in left}) < MIN_LEAF_MATCHES or
                len({str(rows[i]['match_id']) for i in right}) < MIN_LEAF_MATCHES):
            continue
        left_delta, right_delta = _leaf_delta(rows, left), _leaf_delta(rows, right)
        objective = _loss(rows, left, left_delta) + _loss(rows, right, right_delta)
        candidate = (objective, threshold, left_delta, right_delta)
        if best is None or candidate < best:
            best = candidate
    if best is None:
        raise ValueError('比赛分组不足，无法形成有效树叶')
    _, threshold, left_delta, right_delta = best
    return {'architecture': 'market-offset logistic decision stump', 'feature': feature,
            'threshold': threshold, 'left_delta': left_delta, 'right_delta': right_delta,
            'depth': 1, 'leaves': 2, 'parameter_count': 3,
            'training_matches': len({str(r['match_id']) for r in rows}),
            'training_decisions': len(rows), 'production_eligible': False}


def predict_market_stump(model: Mapping[str, Any], baseline: float, feature_value: float) -> float:
    if model.get('architecture') != 'market-offset logistic decision stump':
        raise ValueError('不支持的挑战模型')
    delta = float(model['left_delta'] if feature_value <= float(model['threshold']) else model['right_delta'])
    return _sigmoid(_logit(baseline) + delta)


def visible_match_events(events: Sequence[Mapping[str, Any]], cutoff_ms: float) -> list[dict[str, Any]]:
    """Return only events both occurred and arrived by the decision cutoff."""
    visible = []
    for event in events:
        event_at, received_at = event.get('event_at_ms'), event.get('received_at_ms')
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (event_at, received_at)):
            raise ValueError('事件必须同时含 event_at_ms 与 received_at_ms')
        event_at, received_at = float(cast(float, event_at)), float(cast(float, received_at))
        if event_at <= 0 or received_at <= 0:
            raise ValueError('赛事事件时间无效')
        if event_at <= cutoff_ms and received_at <= cutoff_ms:
            visible.append(dict(event))
    return visible


def dynamic_poisson_distribution(home_rate: float, away_rate: float, elapsed_s: float,
                                 score: tuple[int, int], events: Sequence[Mapping[str, Any]],
                                 cutoff_ms: float) -> tuple[tuple[int, int, float], ...]:
    """Synthetic event-aware remaining-goal challenger with strict event as-of.

    Rates are full-match expected goals. The red-card coefficient is a fixed
    sensitivity knob for experiments, not a learned or production parameter.
    """
    if (not math.isfinite(elapsed_s) or elapsed_s < 0 or elapsed_s >= REGULATION_SECONDS
            or len(score) != 2 or any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in score)):
        raise ValueError('比赛状态无效')
    visible = visible_match_events(events, cutoff_ms)
    home_red = away_red = 0
    for event in visible:
        if event.get('type') == 'red_card':
            if event.get('side') == 'home':
                home_red += 1
            elif event.get('side') == 'away':
                away_red += 1
            else:
                raise ValueError('红牌事件缺少合法 side')
    remaining = (REGULATION_SECONDS - elapsed_s) / REGULATION_SECONDS
    home_lambda = home_rate * remaining * (1 + RED_CARD_RATE_EFFECT * (away_red - home_red))
    away_lambda = away_rate * remaining * (1 + RED_CARD_RATE_EFFECT * (home_red - away_red))
    return remaining_distribution(max(0.0, home_lambda), max(0.0, away_lambda), score)
