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


def observation_weight(row: Mapping[str, Any]) -> float:
    weight = float(row.get('observation_weight', 1.0))
    if not math.isfinite(weight) or not 0 < weight <= 1:
        raise ValueError('无效非走盘本金权重')
    return weight


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
    for row in rows:
        mid, at = str(row['match_id']), instant(row['at'])
        if instant(row['settled_at']) <= at:
            raise ValueError('标签早于或等于输入截点')
        times[mid] = min(times.get(mid, at), at)
    return sorted(times, key=lambda mid: (times[mid], mid))


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
        if len({r['match_id'] for r in training}) >= 30 and len({r['y'] for r in training}) == 2:
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


def evaluate(rows: Sequence[Mapping[str, Any]], probabilities: Sequence[float]) -> dict[str, Any]:
    if not rows:
        raise ValueError('评估样本为空')
    ps = coherent_probabilities(rows, probabilities)
    counts: dict[str, float] = defaultdict(float)
    for row in rows:
        if row['y'] not in (0, 1):
            raise ValueError('无效训练标签')
        counts[str(row['match_id'])] += observation_weight(row)
    accuracy = brier = loss = 0.0
    bins = [[0.0, 0.0, 0.0] for _ in range(10)]
    by_market: dict[str, list[tuple[Mapping[str, Any], float]]] = defaultdict(list)
    had_groups: dict[tuple, list[int]] = defaultdict(list)
    for i, (row, probability) in enumerate(zip(rows, ps)):
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
    return {'matches': len(counts), 'decisions': len(rows), 'accuracy': accuracy,
            'binary_accuracy': accuracy, 'accuracy_basis': '按比赛等权的方向二分类；不是推荐命中率',
            'coverage': 1.0,
            'had_top1_accuracy': sum(sum(v) / len(v) for v in top1.values()) / len(top1) if top1 else None,
            'had_top1_matches': len(top1), 'brier': brier, 'log_loss': loss,
            'ece': sum(w * abs(p / w - y / w) for w, p, y in bins if w),
            'reliability': reliability, 'by_market': per_market,
            'complete_had_groups': sum(len(v) for v in top1.values())}


def paired_evidence(rows: Sequence[Mapping[str, Any]], candidate: Sequence[float], baseline: Sequence[float]) -> dict[str, Any]:
    ps, bs = coherent_probabilities(rows, candidate), coherent_probabilities(rows, baseline)
    deltas: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row, p, b in zip(rows, ps, bs):
        deltas[str(row['match_id'])].append((observation_weight(row), (p - row['y']) ** 2 - (b - row['y']) ** 2))
    values = [sum(w * delta for w, delta in items) / sum(w for w, _ in items) for items in deltas.values()]
    if not values:
        raise ValueError('配对评估样本为空')
    rng = random.Random(VALIDATION_SEED)
    draws = sorted(sum(rng.choice(values) for _ in values) / len(values) for _ in range(BOOTSTRAP_REPETITIONS))
    return {'matches': len(values), 'delta_brier': sum(values) / len(values),
            'ci95': [draws[int(.025 * BOOTSTRAP_REPETITIONS)], draws[int(.975 * BOOTSTRAP_REPETITIONS)]],
            'method': 'paired match-cluster bootstrap', 'repetitions': BOOTSTRAP_REPETITIONS,
            'seed': VALIDATION_SEED}


def promotion(test: Mapping[str, Any], baseline: Mapping[str, Any], evidence: Mapping[str, Any], folds: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    reasons = []
    if evidence['matches'] < MIN_PROMOTION_MATCHES:
        reasons.append(f'独立测试比赛不足{MIN_PROMOTION_MATCHES}场')
    if len(folds) < 3 or sum(f.get('delta_brier', 0) < 0 for f in folds) < 2:
        reasons.append('滚动时间验证没有稳定优于市场')
    if evidence['ci95'][1] >= 0:
        reasons.append('按比赛配对Brier区间尚未证明优于市场')
    if test['log_loss'] > baseline['log_loss']:
        reasons.append('测试log loss劣于市场')
    for market, score in test.get('by_market', {}).items():
        ref = baseline.get('by_market', {}).get(market)
        if ref and score['matches'] >= 20 and score['brier'] > ref['brier'] + .01:
            reasons.append(market + '测试表现明显退化')
    return {'approved': not reasons, 'reasons': reasons, 'evidence': dict(evidence),
            'minimum_test_matches': MIN_PROMOTION_MATCHES, 'basis': '独立测试与按场置信区间；前瞻表现另行记录'}
