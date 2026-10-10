"""Prospective economic pooling with match-clustered performance shrinkage."""
from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Sequence


def algorithm_alerts(algorithms: Sequence[str], evidence: Mapping[str, Any],
                     min_samples: int = 30, threshold: float = .5,
                     enabled: bool = True) -> list[Dict[str, Any]]:
    """Return low-accuracy warnings from settled real-football evidence.

    Evidence is grouped by match, so each row is one independent match
    observation.  Alerts never change weights or disable an algorithm.
    """
    if not enabled:
        return []
    buckets: Dict[str, list] = {a: [] for a in algorithms}
    for row in evidence.get('rows', []):
        algorithm = row.get('algorithm')
        if algorithm in buckets:
            buckets[algorithm].append(row)
    alerts: list[Dict[str, Any]] = []
    for algorithm, rows in buckets.items():
        # Evidence rows are already clustered by match. Split outcomes affect
        # accuracy, but must not reduce the number of effective matches.
        samples = len(rows)
        wins = sum(float(r.get('wins') or 0) for r in rows)
        accuracy = wins / samples if samples else None
        item: Dict[str, Any] = {
            'algorithm': algorithm, 'samples': samples,
            'accuracy': round(accuracy, 6) if accuracy is not None else None,
            'threshold': threshold,
            'status': 'insufficient' if samples < min_samples else 'ok',
        }
        if samples >= min_samples and accuracy is not None and accuracy < threshold:
            item.update(status='warning', severity='warning',
                        message='过去%d个有效样本正确率%.1f%%，低于%.1f%%' %
                        (samples, accuracy * 100, threshold * 100))
        elif samples < min_samples:
            item['message'] = '有效样本不足（%d/%d）' % (samples, min_samples)
        else:
            item['message'] = '正确率未低于阈值'
        alerts.append(item)
    return alerts


def adaptive_weights(algorithms: Sequence[str], evidence: Mapping[str, Any],
                     prior_matches: float = 20.0, max_weight: float = .6) -> Dict[str, Any]:
    buckets: Dict[str, list] = {a: [] for a in algorithms}
    for row in evidence.get('rows', []):
        if row.get('algorithm') in buckets:
            buckets[row['algorithm']].append(row)
    scores, details = {}, {}
    for algorithm, rows in buckets.items():
        n = len(rows)
        win, loss = sum(r['wins'] for r in rows), sum(r['losses'] for r in rows)
        posterior = (win + prior_matches / 2) / (win + loss + prior_matches)
        brier = sum(r['brier'] for r in rows) / n if n else None
        market = sum(r['market_brier'] for r in rows) / n if n else None
        skill = ((float(market) - float(brier)) * n / (n + prior_matches)) if market is not None and brier is not None else 0.0
        # Shrink accuracy AND price-relative proper-score skill; cap noisy evidence.
        scores[algorithm] = min(2.0, max(.25, posterior / .5 * math.exp(3 * skill)))
        details[algorithm] = {'matches': n, 'posterior_accuracy': round(posterior, 6),
                              'brier': brier, 'market_brier': market, 'skill': round(skill, 6)}
    weights, remaining = {}, set(scores)
    cap = max(max_weight, 1 / max(1, len(scores)))
    budget = 1.0
    while remaining:
        total = sum(scores[a] for a in remaining)
        oversized = {a for a in remaining if budget * scores[a] / total > cap}
        if not oversized:
            weights.update({a: budget * scores[a] / total for a in remaining})
            break
        for a in oversized:
            weights[a] = cap
            budget -= cap
        remaining -= oversized
    for a, weight in weights.items():
        details[a]['weight'] = round(weight, 6)
    return {'at': evidence.get('at', ''), 'weights': weights, 'algorithms': details,
            'basis': '真实足球已结算首判；每场聚类；Beta先验与相对市场Brier收缩',
            'prior_matches': prior_matches}


def pool(evaluations: Sequence[Mapping[str, Any]], performance: Mapping[str, Any],
         cfg: Any) -> Dict[str, Any]:
    groups: Dict[tuple, list] = {}
    for evaluation in evaluations:
        for c in evaluation['candidates']:
            groups.setdefault((c['market'], c['line'], c['outcome'], c['odds']), []).append(c)
    all_candidates = []
    weights = performance['weights']
    for members in groups.values():
        total = sum(weights.get(c['algorithm'], 0) for c in members)
        if total <= 0:
            continue
        local = {c['algorithm']: weights[c['algorithm']] / total for c in members}
        def mean(key: str, local: Any = local, members: Any = members) -> float:
            return sum(local[c['algorithm']] * float(c.get(key) or 0) for c in members)
        p = mean('p_model')
        dispersion = math.sqrt(sum(local[c['algorithm']] * (c['p_model']-p)**2 for c in members))
        accuracy = sum(local[c['algorithm']] * performance['algorithms'][c['algorithm']]['posterior_accuracy'] for c in members)
        penalty = cfg.risk_correlation * dispersion * members[0]['odds']
        effective = mean('effective_ev') - penalty
        c = dict(members[0])
        c.update(algorithm='economic_ensemble', p_model=round(p,6), ev=round(mean('ev'),6),
                 effective_ev=round(effective,6), confidence=round(max(0,accuracy*(1-min(1,2*dispersion))),6),
                 disagreement=round(dispersion,6), weights={a:round(w,6) for a,w in local.items()},
                 evidence_at=performance.get('at',''), members=[{'algorithm':m['algorithm'],
                    'p_model':m['p_model'], 'ev':m['ev'], 'weight':round(local[m['algorithm']],6)} for m in members],
                 kelly=round(max(0,mean('kelly'))*(1-cfg.risk_correlation),6),
                 research_only=any(m['research_only'] for m in members),
                 tail=max(m['tail'] for m in members))
        all_candidates.append(c)
    markets: Dict[tuple, list] = {}
    for c in all_candidates:
        markets.setdefault((c['market'],c['line']), []).append(c)
    forecasts = [max(items,key=lambda c:c['p_model']) for items in markets.values()]
    recommendations = []
    for items in markets.values():
        eligible = [c for c in items if not c['research_only'] and c['p_model'] >= cfg.min_probability
                    and c['effective_ev'] >= cfg.min_ev and c['tail'] < .001 and c['kelly'] > 0]
        if eligible:
            recommendations.append(dict(max(eligible,key=lambda c:c['effective_ev'])))
    total_exposure = sum(c['kelly'] for c in recommendations)
    scale = min(1, cfg.max_total_exposure/total_exposure) if total_exposure else 0
    for c in recommendations:
        # Floor preserves a hard portfolio cap after rounding.
        c['kelly'] = math.floor(c['kelly']*scale*1e6)/1e6
    recommendations = [c for c in recommendations if c['kelly']>0]
    all_candidates.sort(key=lambda c:c['effective_ev'],reverse=True)
    recommendations.sort(key=lambda c:c['effective_ev'],reverse=True)
    return {'algorithm':'economic_ensemble', 'candidates':all_candidates,
            'forecasts':forecasts, 'recommendations':recommendations,
            'performance':performance, 'total_exposure':sum(c['kelly'] for c in recommendations)}
