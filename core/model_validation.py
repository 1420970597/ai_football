"""Grouped chronological validation and coherent probability evaluation."""
from __future__ import annotations

import math
import random
from collections import defaultdict
from datetime import datetime
from typing import Any, Mapping, Sequence

from core.settlement import split_line

BOOTSTRAP_REPETITIONS = 300
VALIDATION_SEED = 44001
MIN_PROMOTION_MATCHES = 100
EXECUTION_ALGORITHM = 'economic_ensemble'
# Input/feature versions describe the stored vector. This separate contract
# describes which prediction population, masks and execution gates certified
# an artifact; an old approval cannot certify changed evaluation semantics.
VALIDATION_CONTRACT_VERSION = 'execution-market-curves-graded-context-v1'


def observation_weight(row: Mapping[str, Any]) -> float:
    weight = float(row.get('observation_weight', 1.0))
    if not math.isfinite(weight) or not 0 < weight <= 1:
        raise ValueError('无效非走盘本金权重')
    return weight


def is_graded_row(row: Mapping[str, Any]) -> bool:
    """Validate the binary-loss mask without inventing labels for pushes.

    Context rows remain part of a complete market curve for prediction and
    coherence, but carry no binary outcome or loss weight. Legacy graded rows
    without an explicit mask retain their original positive-weight contract.
    """
    graded = row.get('graded', True)
    if not isinstance(graded, bool):
        raise ValueError('无效二元标签掩码')
    if not graded:
        weight = float(row.get('observation_weight', 0.0))
        if row.get('y') is not None or not math.isfinite(weight) or weight != 0:
            raise ValueError('曲线上下文必须没有二元标签且损失权重为零')
        return False
    if row.get('y') not in (0, 1):
        raise ValueError('无效训练标签')
    observation_weight(row)
    return True


def instant(value: str) -> float:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError('时间必须带时区')
    return dt.timestamp()


def temporal_split(rows: Sequence[Mapping[str, Any]], ids: set[str], cutoff: str) -> list[dict[str, Any]]:
    boundary = instant(cutoff)
    return [dict(r) for r in rows if str(r['match_id']) in ids and instant(r['settled_at']) < boundary]


def ordered_matches(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    times: dict[str, float] = {}
    graded_ids: set[str] = set()
    for row in rows:
        mid, at = str(row['match_id']), instant(row['at'])
        if instant(row['settled_at']) <= at:
            raise ValueError('标签早于或等于输入截点')
        times[mid] = min(times.get(mid, at), at)
        if is_graded_row(row):
            graded_ids.add(mid)
    return sorted(graded_ids, key=lambda mid: (times[mid], mid))


def rolling_folds(rows: Sequence[Mapping[str, Any]], count: int = 3) -> list[tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
    mids = ordered_matches(rows)
    warmup = max(30, len(mids) // 2)
    boundaries = [warmup + (len(mids) - warmup) * i // count for i in range(count + 1)]
    folds = []
    for start, end in zip(boundaries, boundaries[1:]):
        if start == end:
            continue
        validation = [dict(r) for r in rows if str(r['match_id']) in set(mids[start:end])]
        cutoff = min(validation, key=lambda r: instant(r['at']))['at']
        training = temporal_split(rows, set(mids[:start]), cutoff)
        graded_training = [r for r in training if is_graded_row(r)]
        if (len({r['match_id'] for r in graded_training}) >= 30
                and len({r['y'] for r in graded_training}) == 2):
            folds.append((training, validation))
    return folds


def _isotonic(values: Sequence[float], increasing: bool) -> list[float]:
    blocks: list[list[float]] = []
    for value in values:
        blocks.append([value if increasing else -value, 1.0])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            right = blocks.pop()
            blocks[-1][0] += right[0]
            blocks[-1][1] += right[1]
    result = [total / count * (1 if increasing else -1) for total, count in blocks for _ in range(int(count))]
    return result


def coherent_probabilities(rows: Sequence[Mapping[str, Any]], probabilities: Sequence[float]) -> list[float]:
    if len(rows) != len(probabilities):
        raise ValueError('预测数量与输入不一致')
    result = [float(p) for p in probabilities]
    if any(not math.isfinite(p) or not 0 <= p <= 1 for p in result):
        raise ValueError('预测概率无效')
    groups: dict[tuple, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        if row.get('market') and row.get('outcome'):
            groups[(str(row.get('match_id', '')), row.get('at', ''), row.get('algorithm', ''),
                    row['market'], str(row.get('line', '')))].append(i)
    curves: dict[tuple, list[tuple[float, int, int]]] = defaultdict(list)
    for key, indices in groups.items():
        market = key[3]
        expected = ('home', 'draw', 'away') if market.startswith('HAD') else (
            ('over', 'under') if market.startswith('OU') else ('home', 'away'))
        by_outcome = {rows[i]['outcome']: i for i in indices}
        if len(indices) != len(expected) or set(by_outcome) != set(expected):
            continue  # incomplete archived groups retain honest binary metrics
        total = sum(result[i] for i in indices)
        if total <= 0:
            raise ValueError('完整盘口概率之和为零')
        for i in indices:
            result[i] /= total
        if not market.startswith('HAD'):
            parts = split_line(key[4])
            if parts:
                curves[key[:4]].append((sum(parts) / len(parts), by_outcome[expected[0]], by_outcome[expected[1]]))
    for key, curve in curves.items():
        curve.sort()
        projected = _isotonic([result[i] for _, i, _ in curve], increasing=key[3].startswith('AH'))
        for (_, first, second), probability in zip(curve, projected):
            result[first], result[second] = probability, 1 - probability
    return result


def _evaluate(rows: Sequence[Mapping[str, Any]], probabilities: Sequence[float]) -> dict[str, Any]:
    if not rows:
        raise ValueError('评估样本为空')
    ps = coherent_probabilities(rows, probabilities)
    graded_indices = [i for i, row in enumerate(rows) if is_graded_row(row)]
    context_fields = {'prediction_context_decisions': len(rows),
                      'excluded_from_binary_loss': len(rows) - len(graded_indices)}
    if not graded_indices:
        return {'matches': 0, 'decisions': 0, 'accuracy': None, 'binary_accuracy': None,
                'accuracy_basis': '按比赛等权的方向二分类；不是推荐命中率',
                'coverage': 0.0, 'had_top1_accuracy': None, 'had_top1_matches': 0,
                'brier': None, 'log_loss': None, 'ece': None, 'reliability': [],
                'by_market': {}, 'complete_had_groups': 0, **context_fields}
    counts: dict[str, float] = defaultdict(float)
    for i in graded_indices:
        row = rows[i]
        counts[str(row['match_id'])] += observation_weight(row)
    accuracy = brier = loss = 0.0
    bins = [[0.0, 0.0, 0.0] for _ in range(10)]
    by_market: dict[str, list[tuple[Mapping[str, Any], float]]] = defaultdict(list)
    had_groups: dict[tuple, list[int]] = defaultdict(list)
    for i in graded_indices:
        row, probability = rows[i], ps[i]
        weight = observation_weight(row) / counts[str(row['match_id'])] / len(counts)
        y = row['y']
        p = min(1 - 1e-12, max(1e-12, probability))
        accuracy += weight * ((probability >= .5) == bool(y))
        brier += weight * (probability - y) ** 2
        loss -= weight * (y * math.log(p) + (1 - y) * math.log1p(-p))
        bucket = bins[min(9, int(probability * 10))]
        bucket[0] += weight
        bucket[1] += weight * probability
        bucket[2] += weight * y
        market = str(row.get('market') or 'unknown')
        by_market[market].append((row, probability))
        if market.startswith('HAD'):
            had_groups[(row['match_id'], row.get('at'), market, row.get('line'), row.get('algorithm'))].append(i)
    top1: dict[str, list[float]] = defaultdict(list)
    for key, indices in had_groups.items():
        if len(indices) == 3 and {rows[i].get('outcome') for i in indices} == {'home', 'draw', 'away'} and sum(rows[i]['y'] for i in indices) == 1:
            chosen = max(indices, key=lambda i: ps[i])
            top1[str(key[0])].append(float(rows[chosen]['y']))
    reliability = [{'bin': i, 'weight': w, 'mean_probability': p / w, 'observed_paid_win_rate': y / w}
                   for i, (w, p, y) in enumerate(bins) if w]
    per_market = {}
    for market, items in by_market.items():
        totals: dict[str, float] = defaultdict(float)
        for row, _ in items:
            totals[str(row['match_id'])] += observation_weight(row)
        per_market[market] = {'matches': len(totals), 'decisions': len(items),
            'brier': sum(observation_weight(r) / totals[str(r['match_id'])] / len(totals) * (p - r['y']) ** 2 for r, p in items)}
    return {'matches': len(counts), 'decisions': len(graded_indices), 'accuracy': accuracy,
            'binary_accuracy': accuracy, 'accuracy_basis': '按比赛等权的方向二分类；不是推荐命中率',
            'coverage': len(graded_indices) / len(rows),
            'had_top1_accuracy': sum(sum(v) / len(v) for v in top1.values()) / len(top1) if top1 else None,
            'had_top1_matches': len(top1), 'brier': brier, 'log_loss': loss,
            'ece': sum(w * abs(p / w - y / w) for w, p, y in bins if w),
            'reliability': reliability, 'by_market': per_market,
            'complete_had_groups': sum(len(v) for v in top1.values()), **context_fields}


def evaluate(rows: Sequence[Mapping[str, Any]], probabilities: Sequence[float]) -> dict[str, Any]:
    """Report pooled scores and each algorithm's own match-weighted scores.

    An auxiliary algorithm has a different one-hot/algorithm-probability input
    from the ensemble used by live execution. Its improvement must therefore
    remain separately visible instead of certifying the execution subgroup.
    """
    result = _evaluate(rows, probabilities)
    positions: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        positions[str(row.get('algorithm') or 'unknown')].append(index)
    result['by_algorithm'] = {
        algorithm: _evaluate([rows[i] for i in indices], [probabilities[i] for i in indices])
        for algorithm, indices in positions.items()}
    return result


def paired_evidence(rows: Sequence[Mapping[str, Any]], candidate: Sequence[float], baseline: Sequence[float]) -> dict[str, Any]:
    ps, bs = coherent_probabilities(rows, candidate), coherent_probabilities(rows, baseline)
    deltas: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row, p, b in zip(rows, ps, bs):
        if not is_graded_row(row):
            continue
        deltas[str(row['match_id'])].append((observation_weight(row), (p - row['y']) ** 2 - (b - row['y']) ** 2))
    values = [sum(w * delta for w, delta in items) / sum(w for w, _ in items) for items in deltas.values()]
    if not values:
        return {'matches': 0, 'delta_brier': None, 'ci95': [None, None],
                'method': 'paired match-cluster bootstrap', 'repetitions': 0,
                'seed': VALIDATION_SEED, 'reason': '完整预测上下文没有可计二元损失的样本'}
    rng = random.Random(VALIDATION_SEED)
    draws = sorted(sum(rng.choice(values) for _ in values) / len(values) for _ in range(BOOTSTRAP_REPETITIONS))
    return {'matches': len(values), 'delta_brier': sum(values) / len(values),
            'ci95': [draws[int(.025 * BOOTSTRAP_REPETITIONS)], draws[int(.975 * BOOTSTRAP_REPETITIONS)]],
            'method': 'paired match-cluster bootstrap', 'repetitions': BOOTSTRAP_REPETITIONS,
            'seed': VALIDATION_SEED}


def execution_evidence(rows: Sequence[Mapping[str, Any]], candidate: Sequence[float],
                       baseline: Sequence[float]) -> dict[str, Any]:
    """Certify only the actual live ensemble route on identical held-out rows."""
    if len(rows) != len(candidate) or len(rows) != len(baseline):
        raise ValueError('执行子群预测数量与输入不一致')
    indices = [i for i, row in enumerate(rows) if row.get('algorithm') == EXECUTION_ALGORITHM]
    available = any(is_graded_row(rows[i]) for i in indices)
    result: dict[str, Any] = {'algorithm': EXECUTION_ALGORITHM,
                             'scope': 'chronological final fold; identical execution-contract rows',
                             'available': available}
    if not available:
        return {**result, 'reason': '最终独立测试缺少实际执行算法样本'}
    selected = [rows[i] for i in indices]
    ps, bs = [candidate[i] for i in indices], [baseline[i] for i in indices]
    return {**result, 'model': _evaluate(selected, ps), 'baseline': _evaluate(selected, bs),
            'evidence': paired_evidence(selected, ps, bs)}


def _score_reasons(test: Mapping[str, Any], baseline: Mapping[str, Any],
                   evidence: Mapping[str, Any], prefix: str = '') -> list[str]:
    reasons = []
    matches = evidence.get('matches')
    if isinstance(matches, bool) or not isinstance(matches, int) or matches < MIN_PROMOTION_MATCHES:
        reasons.append(prefix + f'独立测试比赛不足{MIN_PROMOTION_MATCHES}场')
    interval = evidence.get('ci95')
    delta = evidence.get('delta_brier')
    if (not isinstance(interval, (list, tuple)) or len(interval) != 2
            or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in interval)
            or interval[0] > interval[1] or interval[1] >= 0 or not isinstance(delta, (int, float))
            or not math.isfinite(delta) or delta >= 0):
        reasons.append(prefix + '按比赛配对Brier区间尚未证明优于市场')
    loss, baseline_loss = test.get('log_loss'), baseline.get('log_loss')
    if (not isinstance(loss, (int, float)) or not math.isfinite(loss) or loss < 0
            or not isinstance(baseline_loss, (int, float)) or not math.isfinite(baseline_loss)
            or baseline_loss < 0):
        reasons.append(prefix + '测试log loss缺少有效标签')
    elif loss > baseline_loss:
        reasons.append(prefix + '测试log loss劣于市场')
    for market, score in test.get('by_market', {}).items():
        ref = baseline.get('by_market', {}).get(market)
        if ref and score['matches'] >= 20 and score['brier'] > ref['brier'] + .01:
            reasons.append(prefix + market + '测试表现明显退化')
    return reasons


def promotion(test: Mapping[str, Any], baseline: Mapping[str, Any], evidence: Mapping[str, Any], folds: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    reasons = _score_reasons(test, baseline, evidence)
    stable_folds = sum(isinstance(f.get('delta_brier'), (int, float))
                       and math.isfinite(f['delta_brier']) and f['delta_brier'] < 0
                       for f in folds)
    if len(folds) < 3 or stable_folds < 2:
        reasons.append('滚动时间验证没有稳定优于市场')
    execution = evidence.get('execution_cohort')
    execution_reasons = []
    if (not isinstance(execution, Mapping) or not execution.get('available')
            or execution.get('algorithm') != EXECUTION_ALGORITHM
            or any(not isinstance(execution.get(key), Mapping) for key in ('model', 'baseline', 'evidence'))):
        execution_reasons.append('实际执行算法缺少独立测试证据，不能晋级')
    else:
        execution_reasons = _score_reasons(execution['model'], execution['baseline'],
                                          execution['evidence'], '实际执行算法：')
    reasons.extend(execution_reasons)
    return {'approved': not reasons, 'reasons': reasons, 'evidence': dict(evidence),
            'validation_contract_version': VALIDATION_CONTRACT_VERSION,
            'execution_required': True, 'execution_algorithm': EXECUTION_ALGORITHM,
            'execution_approved': not execution_reasons,
            'minimum_test_matches': MIN_PROMOTION_MATCHES,
            'basis': '独立测试与按场置信区间；实际执行算法单独过门槛；前瞻表现另行记录'}
