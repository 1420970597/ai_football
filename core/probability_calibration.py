"""Small, dependency-free probability calibrator for out-of-fold predictions."""
from __future__ import annotations

import math
import os
from typing import Any, Mapping, Sequence

MIN_SAMPLES = 30
EPS = 1e-6


def _logit(p: float) -> float:
    if not math.isfinite(float(p)) or not 0 <= float(p) <= 1:
        raise ValueError('校准概率必须为有限的0至1数值')
    p = min(1 - EPS, max(EPS, float(p)))
    return math.log(p / (1 - p))


def calibrate_probability(p: float, calibration: Mapping[str, Any] | None) -> float:
    """Apply a logistic slope/intercept, or identity when unavailable."""
    if not calibration or calibration.get('method') in (None, 'identity'):
        _logit(p)
        return min(1 - EPS, max(EPS, float(p)))
    if calibration.get('method') != 'logistic':
        raise ValueError('未知概率校准方法')
    slope = float(calibration['slope'])
    intercept = float(calibration['intercept'])
    if not all(math.isfinite(v) for v in (slope, intercept)) or slope <= 0:
        raise ValueError('概率校准参数无效')
    z = max(-40, min(40, intercept + slope * _logit(p)))
    return min(1 - EPS, max(EPS, 1 / (1 + math.exp(-z))))


CALIBRATION_EPOCHS = max(1, int(os.environ.get('MODEL_CALIBRATION_EPOCHS', '40')))


def fit_logistic(probs: Sequence[float], outcomes: Sequence[int], *, epochs: int = CALIBRATION_EPOCHS) -> dict[str, Any]:
    """Fit Platt-style calibration with two parameters using Newton updates.

    The returned identity artifact is explicit when data are too sparse or
    degenerate, so a failed calibration can never silently distort inference.
    """
    if len(probs) != len(outcomes):
        raise ValueError('校准输入长度不一致')
    if any(y not in (0, 1) for y in outcomes):
        raise ValueError('校准标签必须为0或1')
    xs = [_logit(float(p)) for p in probs]
    ys = [int(y) for y in outcomes]
    if len(probs) < MIN_SAMPLES or len(set(ys)) < 2:
        return {'method': 'identity', 'samples': len(probs), 'reason': '样本不足或标签单一'}
    slope, intercept = 1.0, 0.0
    for _ in range(max(1, int(epochs))):
        g0 = g1 = h00 = h01 = h11 = 0.0
        for x, y in zip(xs, ys):
            p = 1 / (1 + math.exp(-max(-40, min(40, intercept + slope * x))))
            weight = max(EPS, p * (1 - p))
            error = p - y
            g0 += error
            g1 += error * x
            h00 += weight
            h01 += weight * x
            h11 += weight * x * x
        determinant = h00 * h11 - h01 * h01
        if determinant <= EPS or not math.isfinite(determinant):
            return {'method': 'identity', 'samples': len(probs), 'reason': '校准矩阵退化'}
        step_intercept = (h11 * g0 - h01 * g1) / determinant
        step_slope = (-h01 * g0 + h00 * g1) / determinant
        intercept -= max(-1, min(1, step_intercept))
        slope -= max(-1, min(1, step_slope))
        slope = max(.05, min(20, slope))
        if abs(step_intercept) + abs(step_slope) < 1e-8:
            break
    if not all(math.isfinite(v) for v in (slope, intercept)):
        return {'method': 'identity', 'samples': len(probs), 'reason': '校准参数非有限'}
    return {'method': 'logistic', 'slope': slope, 'intercept': intercept, 'samples': len(probs),
            'source': 'rolling out-of-fold predictions'}
