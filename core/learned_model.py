"""Regularized supervised directional calibration, independent of bet execution."""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from core.decision_features import (EXTRA_FEATURES, extra_features, residual_vector,
                                    upgrade_vector)
from core.decision_features import FEATURE_VERSION, visible_history

BASE_FEATURES = ('bias', 'market_logit', 'algorithm_logit', 'elapsed', 'score_difference',
            'home', 'draw', 'away', 'over', 'under', 'ou', 'ah', 'half', 'line',
            'trend_return', 'trend_missing', 'trend_volatility', 'trend_points', 'poisson_market', 'poisson_time_decay',
            'devig_consensus', 'economics_risk_adjusted', 'microstructure_adjusted', 'economic_ensemble',
            'peer_poisson_market', 'peer_poisson_time_decay', 'peer_devig_consensus',
            'peer_economics_risk_adjusted', 'peer_microstructure_adjusted', 'peer_missing')
FEATURES = BASE_FEATURES + EXTRA_FEATURES
MIN_MATCHES = 100
# The learner is intentionally small and runs in a dependency-free worker.
# Keep the iteration budget bounded: rolling validation fits several models
# and an uncached 250-epoch Python loop exceeded the worker wall-clock limit.
EPOCHS = 80
LEARNING_RATE = 0.08
REGULARIZATION = 0.01
DATA_VERSION = 'remaining-ah-signed-lines-bernoulli-v4-offset-windows-asof'


def compatible_model(model: Mapping[str, Any]) -> bool:
    return (model.get('data_version') == DATA_VERSION and
            model.get('feature_version') == FEATURE_VERSION and
            model.get('features') == list(FEATURES) and bool(model.get('version')))


def observation_weight(row: Mapping[str, Any]) -> float:
    weight = float(row.get('observation_weight', 1.0))
    if not math.isfinite(weight) or not 0 < weight <= 1:
        raise ValueError('无效非走盘本金权重')
    return weight


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-max(-40, min(40, x))))


def features(row: Mapping[str, Any]) -> list[float]:
    if row.get('market') not in ('HAD', 'OU', 'AH', 'HAD_1H', 'OU_1H', 'AH_1H'):
        raise ValueError('不支持的盘口')
    score = row.get('entry_score')
    clock = row.get('entry_clock_s')
    if not isinstance(score, (list, tuple)) or len(score) != 2 or clock is None:
        raise ValueError('缺少决策时赛况')
    elapsed = float(clock)
    if not math.isfinite(elapsed) or not 0 <= elapsed < 5400:
        raise ValueError('无效或终场输入')
    logits = []
    for key in ('p_market', 'p_fused'):
        p = float(row[key])
        if not math.isfinite(p) or not 0 < p < 1:
            raise ValueError('概率缺失或无效')
        p = max(.01, min(.99, p))
        logits.append(math.log(p / (1 - p)))
    diff = float(score[0]) - float(score[1])
    if not math.isfinite(diff):
        raise ValueError('比分无效')
    from core.settlement import split_line
    lines = split_line(row.get('line')) if not row['market'].startswith('HAD') else [0.0]
    trend = row.get('training_trend_pct')
    trend_value = float(trend) if trend is not None else 0.0
    if not lines or not math.isfinite(trend_value):
        raise ValueError('走势或盘口线无效')
    line = sum(lines) / len(lines)
    # Keep every training feature on the exact same as-of history used by the
    # online feature builder. This also excludes pre-cutoff quotes received
    # after the decision cutoff when rows carry a received timestamp.
    history = visible_history(row)
    changes = [math.log(float(b[1]) / float(a[1])) for a, b in zip(history, history[1:])]
    volatility = math.sqrt(sum(v*v for v in changes) / len(changes)) if changes else 0.0
    peer_logits = []
    missing = 0
    peers = row.get('algorithm_probabilities') or {}
    for algorithm in ('poisson_market', 'poisson_time_decay', 'devig_consensus',
                      'economics_risk_adjusted', 'microstructure_adjusted'):
        value = peers.get(algorithm)
        if value is None:
            peer_logits.append(0.0)
            missing += 1
        else:
            p = float(value)
            if not math.isfinite(p) or not 0 <= p <= 1:
                raise ValueError('同一快照算法概率无效')
            p = max(.01, min(.99, p))
            peer_logits.append(math.log(p / (1-p)))
    base = [1, *logits, elapsed / 5400, max(-5, min(5, diff)) / 5,
            *[float(row['outcome'] == outcome) for outcome in ('home', 'draw', 'away', 'over', 'under')],
            float(row['market'].startswith('OU')), float(row['market'].startswith('AH')),
            float(row['market'].endswith('_1H')), max(-10, min(10, line)) / 10,
            max(-1, min(1, trend_value / 100)), float(trend is None),
            min(1, volatility), min(1, len(history) / 40),
            *[float(row.get('algorithm') == a) for a in ('poisson_market', 'poisson_time_decay',
                'devig_consensus', 'economics_risk_adjusted', 'microstructure_adjusted', 'economic_ensemble')],
            *peer_logits, missing / 5]
    if len(base) != len(BASE_FEATURES):
        raise ValueError('基础特征版本不匹配')
    return base + extra_features(row, base)



def predict(weights: Sequence[float], x: Sequence[float]) -> float:
    if len(weights) != len(FEATURES) or len(x) != len(FEATURES):
        raise ValueError('模型特征版本不匹配')
    if not all(math.isfinite(v) for v in [*weights, *x]):
        raise ValueError('无效模型参数')
    vector = upgrade_vector(x)
    residual = residual_vector(vector)
    # The market logit is an offset. Model weights are only allowed to learn a
    # residual; this makes a zero-weight model exactly equal to the market.
    return sigmoid(vector[1] + sum(w * v for w, v in zip(weights, residual)))


def metrics(rows: Sequence[Mapping[str, Any]], weights: Sequence[float] | None) -> dict[str, Any]:
    if not rows:
        raise ValueError('评估样本为空')
    counts: dict[str, float] = {}
    for row in rows:
        counts[row['match_id']] = counts.get(row['match_id'], 0) + observation_weight(row)
    accuracy = brier = logloss = 0.0
    for row in rows:
        p = predict(weights, row['x']) if weights is not None else row['baseline']
        y, weight = row['y'], observation_weight(row) / counts[row['match_id']] / len(counts)
        accuracy += weight * ((p >= .5) == bool(y))
        brier += weight * (p-y)**2
        logloss -= weight * (y*math.log(max(1e-12,p))+(1-y)*math.log(max(1e-12,1-p)))
    return {'matches': len(counts), 'decisions': len(rows), 'accuracy': accuracy,
            'brier': brier, 'log_loss': logloss}


def fit_weights(rows: Sequence[Mapping[str, Any]], *, epochs: int = EPOCHS) -> list[float]:
    """Fit only the residual parameters on an already time-isolated cohort.

    The caller owns the chronological split.  Keeping this primitive separate
    from :func:`train` lets the worker run rolling folds without accidentally
    training on a fold's validation labels.
    """
    if not rows:
        raise ValueError('训练样本为空')
    counts: dict[str, float] = {}
    for row in rows:
        if row['y'] not in (0, 1):
            raise ValueError('无效训练标签')
        predict([0.0] * len(FEATURES), row['x'])
        counts[row['match_id']] = counts.get(row['match_id'], 0) + observation_weight(row)
    if len(counts) < 2:
        raise ValueError('训练比赛不足')
    weights = [0.0] * len(FEATURES)
    # Cache all row-local work once. The previous implementation rebuilt the
    # upgraded feature vector and market logit on every epoch, multiplying
    # the cost of rolling validation by hundreds of millions of operations.
    prepared = []
    for row in rows:
        vector = residual_vector(row['x'])
        market_logit = upgrade_vector(row['x'])[1]
        scale = observation_weight(row) / counts[row['match_id']]
        prepared.append((vector, market_logit, float(row['y']), scale))
    feature_count = len(FEATURES)
    for _ in range(max(1, int(epochs))):
        gradients = [0.0] * len(weights)
        for vector, market_logit, target, scale in prepared:
            score = market_logit
            for i in range(feature_count):
                score += weights[i] * vector[i]
            error = (sigmoid(score) - target) * scale
            for i, value in enumerate(vector):
                gradients[i] += error * value
        weights = [w - LEARNING_RATE * (g / len(counts) + (REGULARIZATION*w if i else 0))
                   for i, (w, g) in enumerate(zip(weights, gradients))]
    return weights



def train(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda r: (r['at'], r['match_id']))
    match_times: dict[str, str] = {}
    for row in ordered:
        match_times.setdefault(row['match_id'], row['at'])
    matches = sorted(match_times, key=lambda mid: (match_times[mid], mid))
    if len(matches) < MIN_MATCHES:
        raise ValueError('至少需要100场有效比赛')
    cut = int(len(matches) * .8)
    training_ids = set(matches[:cut])
    validation = [r for r in ordered if r['match_id'] not in training_ids]
    # At validation entry time, every training label must already be known.
    training = [r for r in ordered if r['match_id'] in training_ids and r['settled_at'] < validation[0]['at']]
    if len({r['match_id'] for r in training}) < 60 or len({r['y'] for r in training}) < 2:
        raise ValueError('时间隔离后训练样本不足或只有一种标签')
    from core.oof_peer import (apply_peer_calibration, crossfit_peer_features,
                               peer_calibrations)
    peer_calibration = peer_calibrations(training)
    training_oof = crossfit_peer_features(training)
    validation_calibrated = apply_peer_calibration(training, validation)
    weights = fit_weights(training_oof)
    return {'architecture': 'L2 Logistic offset + chronological OOF peer residual calibration', 'features': list(FEATURES),
            'data_version': DATA_VERSION,
            'feature_version': FEATURE_VERSION, 'offset': 'market_logit',
            'weights': weights, 'parameter_count': len(weights), 'peer_calibration': peer_calibration,
            'training': metrics(training_oof, weights),
            'validation': metrics(validation_calibrated, weights), 'baseline': metrics(validation, None),
            'validation_from': validation[0]['at'], 'validation_to': validation[-1]['at'],
            'training_cutoff': max(r['settled_at'] for r in training),
            'cohort_ids': matches, 'eligible_matches': len(matches),
            'excluded_by_embargo': cut - len({r['match_id'] for r in training})}
