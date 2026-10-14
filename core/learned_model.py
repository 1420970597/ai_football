"""Regularized supervised directional calibration, independent of bet execution."""
from __future__ import annotations

import math
import os
from typing import Any, Mapping, Sequence

from core.decision_features import (EXTRA_FEATURES, extra_features, residual_vector,
                                    upgrade_vector)
from core.decision_features import FEATURE_VERSION, visible_history
from core.model_validation import (EXECUTION_ALGORITHM, VALIDATION_CONTRACT_VERSION,
                                   evaluate, is_graded_row, promotion)

BASE_FEATURES = ('bias', 'market_logit', 'algorithm_logit', 'elapsed', 'score_difference',
            'home', 'draw', 'away', 'over', 'under', 'ou', 'ah', 'half', 'line',
            'trend_return', 'trend_missing', 'trend_volatility', 'trend_points', 'poisson_market', 'poisson_time_decay',
            'devig_consensus', 'economics_risk_adjusted', 'microstructure_adjusted', 'economic_ensemble',
            'peer_poisson_market', 'peer_poisson_time_decay', 'peer_devig_consensus',
            'peer_economics_risk_adjusted', 'peer_microstructure_adjusted', 'peer_missing')
FEATURES = BASE_FEATURES + EXTRA_FEATURES
MIN_MATCHES = 100
# The learner is intentionally small and runs in a dependency-free worker.
# Rolling validation fits several models.  Keep separate, bounded budgets so
# the 1-core production worker cannot exhaust its RLIMIT_CPU before it writes
# a candidate artifact.  Both values remain environment-tunable for offline
# experiments without changing the production defaults.
EPOCHS = max(1, int(os.environ.get('MODEL_TRAINING_EPOCHS', '24')))
VALIDATION_EPOCHS = max(1, int(os.environ.get('MODEL_VALIDATION_EPOCHS', '12')))
LEARNING_RATE = 0.08
REGULARIZATION = 0.01
DATA_VERSION = 'remaining-ah-signed-lines-bernoulli-v5-offset-windows-asof-quote-qualified'


def compatible_model(model: Mapping[str, Any]) -> bool:
    return not model_compatibility_reason(model)


def model_compatibility_reason(model: Mapping[str, Any], *, require_approval: bool = False) -> str:
    """Explain quarantine without treating old approvals as current evidence."""
    if model.get('data_version') != DATA_VERSION:
        return '训练数据版本不兼容：需要新代报价与赛况资格数据'
    if model.get('feature_version') != FEATURE_VERSION or model.get('features') != list(FEATURES):
        return '模型特征版本不兼容'
    if model.get('validation_contract_version') != VALIDATION_CONTRACT_VERSION:
        return '模型验证契约不兼容：缺少完整盘口上下文与实际执行算法验证'
    if not model.get('version'):
        return '模型版本缺失'
    if not require_approval:
        return ''
    decision = model.get('promotion')
    if not isinstance(decision, Mapping) or decision.get('approved') is not True:
        return '模型尚未通过独立晋级门槛'
    if decision.get('validation_contract_version') != VALIDATION_CONTRACT_VERSION:
        return '模型晋级记录使用旧验证契约'
    if (decision.get('execution_required') is not True
            or decision.get('execution_algorithm') != EXECUTION_ALGORITHM
            or decision.get('execution_approved') is not True):
        return '模型缺少实际执行算法的独立晋级批准'
    test, baseline, evidence, folds = (model.get('validation'), model.get('baseline'),
                                      decision.get('evidence'), model.get('rolling_validation'))
    if (not isinstance(test, Mapping) or not isinstance(baseline, Mapping)
            or not isinstance(evidence, Mapping)
            or not isinstance(folds, list)):
        return '模型晋级证据缺失，不能复核实际执行门槛'
    try:
        reviewed = promotion(test, baseline, evidence, folds)
    except (KeyError, ValueError, TypeError, IndexError, ArithmeticError):
        return '模型晋级证据无效，不能复核实际执行门槛'
    if not reviewed['approved']:
        return '模型晋级证据未通过当前门槛：' + '；'.join(reviewed['reasons'])
    return ''


def approved_model(model: Mapping[str, Any]) -> bool:
    return not model_compatibility_reason(model, require_approval=True)


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
    # Production inference normalizes every complete same-line outcome group
    # before selecting a direction. Apply the same coherence transform here so
    # the artifact's train/validation scores cannot use a different probability
    # space from the live path. Binary accuracy remains a diagnostic metric;
    # HAD top-1 accuracy is reported by ``model_validation.evaluate``.
    raw_probabilities = [predict(weights, row['x']) if weights is not None else row['baseline']
                         for row in rows]
    return evaluate(rows, raw_probabilities)


#: Residual coordinates the learner may move (TASK-61).  A chronological
#: benchmark on the production training set (1,118 matches, 3 rolling folds)
#: found that a market-offset stack over the five algorithms' disagreements
#: beat the market in every fold (pooled delta Brier -0.00084, 95% CI
#: [-0.0017, -0.00001]), while the full 60-coordinate residual fit did not
#: (-0.00007, CI crossing zero).  The other coordinates stay exactly zero, so
#: the artifact format and live inference are unchanged.
STACK_FEATURES = ('bias', 'algorithm_logit', 'peer_poisson_market', 'peer_poisson_time_decay',
                  'peer_devig_consensus', 'peer_economics_risk_adjusted',
                  'peer_microstructure_adjusted')
STACK_INDICES = tuple(FEATURES.index(name) for name in STACK_FEATURES)
STACK_L2 = 1.0
NEWTON_ITERATIONS = 30


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Gauss-Jordan with partial pivoting; singular directions get a zero step."""
    n = len(vector)
    m = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[pivot] = m[pivot], m[col]
        if abs(m[col][col]) < 1e-14:
            continue
        for r in range(n):
            if r != col and m[r][col]:
                factor = m[r][col] / m[col][col]
                for j in range(col, n + 1):
                    m[r][j] -= factor * m[col][j]
    return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-14 else 0.0 for i in range(n)]


def fit_weights(rows: Sequence[Mapping[str, Any]], *, epochs: int = EPOCHS) -> list[float]:
    """Fit the stacked residual parameters on an already time-isolated cohort.

    The caller owns the chronological split.  Only :data:`STACK_INDICES` are
    estimated, by regularized Newton steps on the match-weighted log loss;
    every other coordinate of the returned vector is zero.  ``epochs`` is kept
    for call compatibility and caps the Newton iterations.
    """
    if not rows:
        raise ValueError('训练样本为空')
    graded = []
    counts: dict[str, float] = {}
    for row in rows:
        predict([0.0] * len(FEATURES), row['x'])
        if not is_graded_row(row):
            continue
        graded.append(row)
        counts[row['match_id']] = counts.get(row['match_id'], 0) + observation_weight(row)
    if len(counts) < 2:
        raise ValueError('训练比赛不足')
    k = len(STACK_INDICES)
    prepared = []
    for row in graded:
        vector = residual_vector(row['x'])
        market_logit = upgrade_vector(row['x'])[1]
        scale = observation_weight(row) / counts[row['match_id']] / len(counts)
        prepared.append(([vector[i] for i in STACK_INDICES], market_logit, float(row['y']), scale))
    beta = [0.0] * k
    for _ in range(max(1, min(int(epochs), NEWTON_ITERATIONS))):
        gradient = [0.0] * k
        hessian = [[0.0] * k for _ in range(k)]
        for features_, offset, target, scale in prepared:
            p = sigmoid(offset + sum(b * f for b, f in zip(beta, features_)))
            error, curvature = (p - target) * scale, p * (1 - p) * scale
            for i in range(k):
                gradient[i] += error * features_[i]
                row_h = hessian[i]
                for j in range(k):
                    row_h[j] += curvature * features_[i] * features_[j]
        # Ridge on everything except the intercept, scaled per match.
        ridge = STACK_L2 / len(counts)
        for i in range(1, k):
            gradient[i] += ridge * beta[i]
            hessian[i][i] += ridge
        for i in range(k):
            hessian[i][i] += 1e-12
        step = _solve(hessian, gradient)
        beta = [b - s for b, s in zip(beta, step)]
        if not all(math.isfinite(b) for b in beta):
            raise ValueError('残差堆叠求解发散')
        if sum(abs(s) for s in step) < 1e-9:
            break
    weights = [0.0] * len(FEATURES)
    for index, value in zip(STACK_INDICES, beta):
        weights[index] = value
    return weights



def train(rows: Sequence[Mapping[str, Any]], *,
          partition: tuple[Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]] | None = None,
          epochs: int = EPOCHS) -> dict[str, Any]:
    from core.model_validation import instant, ordered_matches
    ordered = sorted(rows, key=lambda r: (instant(r['at']), str(r['match_id'])))
    matches = ordered_matches(ordered)
    if len(matches) < MIN_MATCHES:
        raise ValueError('至少需要100场有效比赛')
    cut = int(len(matches) * .8)
    if partition is None:
        training_ids = set(matches[:cut])
        validation_ids = set(matches[cut:])
        validation = [r for r in ordered if str(r['match_id']) in validation_ids]
        # At validation entry time, every training label must already be known.
        boundary = instant(validation[0]['at'])
        training = [r for r in ordered if str(r['match_id']) in training_ids
                    and instant(r['settled_at']) < boundary]
    else:
        training, validation = (sorted(cohort, key=lambda r: (instant(r['at']), str(r['match_id'])))
                                for cohort in partition)
        if not training or not validation:
            raise ValueError('明确时间分区不能为空')
        training_ids = {str(r['match_id']) for r in training}
        validation_ids = {str(r['match_id']) for r in validation}
        if training_ids & validation_ids or not training_ids | validation_ids <= set(matches):
            raise ValueError('明确时间分区包含重复比赛或未知比赛')
        boundary = instant(validation[0]['at'])
        if any(instant(r['settled_at']) >= boundary for r in training):
            raise ValueError('时间隔离分区包含测试前尚不可用的标签')
        cut = len(training_ids)
    graded_training = [r for r in training if is_graded_row(r)]
    if len({r['match_id'] for r in graded_training}) < 60 or len({r['y'] for r in graded_training}) < 2:
        raise ValueError('时间隔离后训练样本不足或只有一种标签')
    from core.oof_peer import (apply_peer_calibration, crossfit_peer_features,
                               peer_calibrations)
    peer_calibration = peer_calibrations(training)
    training_oof = crossfit_peer_features(training)
    validation_calibrated = apply_peer_calibration(training, validation)
    weights = fit_weights(training_oof, epochs=epochs)
    return {'architecture': 'Market-offset stacked algorithm residuals (7 active) + chronological OOF peer calibration',
            'active_parameters': list(STACK_FEATURES), 'active_parameter_count': len(STACK_FEATURES),
            'features': list(FEATURES),
            'data_version': DATA_VERSION,
            'validation_contract_version': VALIDATION_CONTRACT_VERSION,
            'feature_version': FEATURE_VERSION, 'offset': 'market_logit',
            'weights': weights, 'parameter_count': len(weights), 'peer_calibration': peer_calibration,
            'training': metrics(training_oof, weights),
            'validation': metrics(validation_calibrated, weights), 'baseline': metrics(validation, None),
            'validation_from': validation[0]['at'], 'validation_to': validation[-1]['at'],
            'training_cutoff': max(training, key=lambda r: instant(r['settled_at']))['settled_at'],
            'training_epochs': max(1, int(epochs)),
            'training_match_ids': sorted(str(mid) for mid in {r['match_id'] for r in graded_training}),
            'validation_match_ids': sorted(str(mid) for mid in {r['match_id'] for r in validation if is_graded_row(r)}),
            'cohort_ids': matches, 'eligible_matches': len(matches),
            'excluded_by_embargo': cut - len({r['match_id'] for r in graded_training})}
