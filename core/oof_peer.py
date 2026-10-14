"""Chronological, match-disjoint out-of-fold peer predictions.

This module deliberately accepts fit/predict callbacks so peer models can be
cross-fitted without coupling feature storage or running training at import
time. Any row without a valid held-out prediction safely uses its market prior.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from core.decision_features import EXTRA_INDEX, PEERS
from core.learned_model import FEATURES
from core.model_validation import is_graded_row
from core.probability_calibration import calibrate_probability, fit_logistic


def _timestamp(value: Any) -> float:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError('时间必须带时区')
    return parsed.timestamp()


def chronological_oof(
    rows: Sequence[Mapping[str, Any]],
    fit: Callable[[list[dict[str, Any]]], Any],
    predict: Callable[[Any, list[dict[str, Any]]], Sequence[float]],
    *,
    folds: int = 3,
    min_training_matches: int = 30,
) -> list[float]:
    """Return held-out predictions in original row order, market-fallback otherwise.

    Match start times determine chronological blocks. Each validation block is
    predicted only by labels from strictly earlier matches; no match can occur
    on both sides of a fold. Labels must become available after feature time.
    """
    if folds < 1 or min_training_matches < 1:
        raise ValueError('folds 与 min_training_matches 必须为正数')
    result: list[float] = []
    starts: dict[str, float] = {}
    graded_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        market = float(row['baseline'])
        if not math.isfinite(market) or not 0 < market < 1:
            raise ValueError('市场概率无效')
        result.append(market)
        match_id = str(row['match_id'])
        at = _timestamp(row['at'])
        label_at = _timestamp(row['settled_at'])
        if label_at <= at:
            raise ValueError('标签早于或等于特征截点')
        starts[match_id] = min(starts.get(match_id, at), at)
        if is_graded_row(row):
            graded_ids.add(match_id)
        normalized.append(row)
    ordered = sorted(graded_ids, key=lambda mid: (starts[mid], mid))
    if len(ordered) <= min_training_matches:
        return result
    warmup = max(min_training_matches, len(ordered) // 2)
    remaining = len(ordered) - warmup
    boundaries = [warmup + remaining * i // folds for i in range(folds + 1)]
    for start, end in zip(boundaries, boundaries[1:]):
        if start >= end:
            continue
        train_ids = set(ordered[:start])
        valid_ids = set(ordered[start:end])
        validation = [r for r in normalized if str(r['match_id']) in valid_ids]
        # Compute the temporal boundary once.  Re-evaluating the minimum
        # validation timestamp inside the training-row comprehension made the
        # cost O(training_rows * validation_rows) for every peer and fold.
        cutoff = min(_timestamp(v['at']) for v in validation)
        training = [r for r in normalized if str(r['match_id']) in train_ids
                    and _timestamp(r['settled_at']) < cutoff and is_graded_row(r)]
        if len({str(r['match_id']) for r in training}) < min_training_matches:
            continue
        if {str(r['match_id']) for r in training} & valid_ids:
            raise AssertionError('OOF fold contains same-match leakage')
        model = fit(training)
        probabilities = list(predict(model, validation))
        if len(probabilities) != len(validation):
            raise ValueError('OOF 预测数量与验证样本不一致')
        for row, probability in zip(validation, probabilities):
            p = float(probability)
            if math.isfinite(p) and 0 < p < 1:
                # Store by object identity to preserve repeated rows and input order.
                row['_oof_probability'] = p
        for index, row in enumerate(normalized):
            if str(row['match_id']) in valid_ids and '_oof_probability' in row:
                result[index] = float(row.pop('_oof_probability'))
    return result


def _sigmoid(value: float) -> float:
    return 1 / (1 + math.exp(-max(-40, min(40, value))))


def _peer_probability(row: Mapping[str, Any], peer: str) -> float | None:
    x = row.get('x')
    if not isinstance(x, (list, tuple)) or len(x) != len(FEATURES):
        return None
    missing_index = EXTRA_INDEX[peer + '_missing']
    if float(x[missing_index]) >= .5:
        return None
    value = float(x[FEATURES.index('peer_' + peer)])
    if not math.isfinite(value):
        return None
    return _sigmoid(value)


def _fit_peer_calibrator(rows: Sequence[Mapping[str, Any]], peer: str) -> dict[str, Any]:
    eligible = [(row, probability) for row in rows
                if is_graded_row(row) and (probability := _peer_probability(row, peer)) is not None]
    return fit_logistic([probability for _, probability in eligible],
                        [int(row['y']) for row, _ in eligible])


def crossfit_peer_features(rows: Sequence[Mapping[str, Any]], *, folds: int = 3,
                           min_training_matches: int = 30) -> list[dict[str, Any]]:
    """Replace peer logits with chronological OOF-calibrated residual inputs.

    Missing, sparse, or warm-up peer predictions collapse exactly to each row's
    market prior. The caller must use these transformed rows to fit its meta
    learner; validation rows require :func:`apply_peer_calibration` using only
    the earlier training cohort.
    """
    output = [dict(row, x=list(row['x'])) for row in rows]
    for peer in PEERS:
        positions = [i for i, row in enumerate(rows) if _peer_probability(row, peer) is not None]
        if not positions:
            continue
        source = []
        for index in positions:
            row = dict(rows[index])
            row['_peer_probability'] = _peer_probability(row, peer)
            source.append(row)

        def fit(training: list[dict[str, Any]]) -> dict[str, Any]:
            return fit_logistic([float(row['_peer_probability']) for row in training],
                                [int(row['y']) for row in training])

        def predict(model: Mapping[str, Any], validation: list[dict[str, Any]]) -> list[float]:
            return [calibrate_probability(float(row['_peer_probability']), model) for row in validation]

        calibrated = chronological_oof(source, fit, predict, folds=folds,
                                       min_training_matches=min_training_matches)
        for index, probability in zip(positions, calibrated):
            output[index]['x'][FEATURES.index('peer_' + peer)] = math.log(
                min(1 - 1e-6, max(1e-6, probability)) /
                (1 - min(1 - 1e-6, max(1e-6, probability))))
    return output


def apply_peer_calibration(training: Sequence[Mapping[str, Any]],
                           validation: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Calibrate future peers from the complete earlier training cohort only."""
    output = [dict(row, x=list(row['x'])) for row in validation]
    for peer in PEERS:
        model = _fit_peer_calibrator(training, peer)
        feature_index = FEATURES.index('peer_' + peer)
        for row in output:
            probability = _peer_probability(row, peer)
            if probability is None:
                continue
            calibrated = calibrate_probability(probability, model)
            calibrated = min(1 - 1e-6, max(1e-6, calibrated))
            row['x'][feature_index] = math.log(calibrated / (1 - calibrated))
    return output


def peer_calibrations(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Fit deployable peer calibrators from an earlier, fully settled cohort."""
    return {peer: _fit_peer_calibrator(rows, peer) for peer in PEERS}


def calibrate_peer_vector(values: Sequence[float],
                          calibrations: Mapping[str, Mapping[str, Any]] | None) -> list[float]:
    """Apply stored per-peer calibration to one feature vector."""
    x = [float(value) for value in values]
    if len(x) != len(FEATURES):
        raise ValueError('模型特征版本不匹配')
    for peer in PEERS:
        calibration = (calibrations or {}).get(peer)
        if not calibration or calibration.get('method') in (None, 'identity'):
            continue
        if x[EXTRA_INDEX[peer + '_missing']] >= .5:
            continue
        index = FEATURES.index('peer_' + peer)
        probability = calibrate_probability(_sigmoid(x[index]), calibration)
        probability = min(1 - 1e-6, max(1e-6, probability))
        x[index] = math.log(probability / (1 - probability))
    return x
