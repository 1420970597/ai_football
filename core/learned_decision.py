"""Apply an approved model or blend its probability in the isolated calculator.

The learner remains a market-offset model with algorithm probabilities as
features. ``model`` selects its output directly; ``hybrid`` explicitly pools
that output with the economic ensemble. Training never runs on this path.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from functools import lru_cache
import json
import math
from pathlib import Path
from typing import Any, Mapping

from core.economics import effective_ev as economics_effective_ev, fractional_kelly, q_fill_model
from core.ensemble import cohere_candidates
from core.learned_model import compatible_model, features, predict
from core.oof_peer import calibrate_peer_vector
from core.decision_features import FEATURE_VERSION, verified_context, visible_history
from core.live_model import clock_seconds
from core.probability_calibration import calibrate_probability


@lru_cache(maxsize=4)
def _load(path: str, signature: tuple[int, int, int]) -> dict[str, Any]:
    model = json.loads(Path(path).read_text())
    if not isinstance(model, dict) or not compatible_model(model):
        raise ValueError('训练数据版本不兼容')
    promotion = model.get('promotion')
    if not isinstance(promotion, dict) or promotion.get('approved') is not True:
        raise ValueError('模型尚未通过独立晋级门槛')
    # Validate the complete parameter vector even when this snapshot contains
    # no supported candidates. A corrupted active file must not appear ready.
    from core.learned_model import FEATURES
    predict(model['weights'], [0.0] * len(FEATURES))
    return model


def calibrate(ensemble: Mapping[str, Any], evaluations: list[dict[str, Any]],
              snapshot: Mapping[str, Any], cfg: Any, model_version: str,
              *, mode: str = 'model', blend_weight: float = 1.0) -> dict[str, Any]:
    out = deepcopy(dict(ensemble))
    application: dict[str, Any] = {
        'mode': mode, 'applied': False, 'available': False, 'state': 'unavailable',
        'observation_shadow': bool(cfg.model_shadow_enabled), 'execution_enabled': False,
        'input_cutoff_ms': snapshot.get('captured_at_ms'),
    }
    out['model_application'] = application
    try:
        if mode not in ('model', 'hybrid'):
            raise ValueError('无效模型决策模式')
        weight = 1.0 if mode == 'model' else float(blend_weight)
        if not math.isfinite(weight) or not 0 <= weight <= 1:
            raise ValueError('模型混合权重必须在0~1之间')
        configured_path = snapshot.get('_model_path')
        if not configured_path:
            raise ValueError('尚无通过晋级的决策模型')
        path = Path(configured_path)
        stat = path.stat()
        model = _load(str(path), (stat.st_mtime_ns, stat.st_ino, stat.st_size))
        cutoff = float(snapshot['captured_at_ms'])
        if not math.isfinite(cutoff) or cutoff <= 0:
            raise ValueError('决策输入截点无效')
        trained_at = datetime.fromisoformat(model['updated_at'])
        if trained_at.tzinfo is None:
            raise ValueError('模型更新时间缺少时区')
        trained_ms = trained_at.timestamp() * 1000
        if trained_ms > cutoff:
            raise ValueError('模型尚未在输入截点前可用')
        if model.get('feature_version') != FEATURE_VERSION:
            raise ValueError('模型特征版本不兼容')
        application.update(available=True, state='ready', version=model['version'],
                           model_version=model_version,
                           weights={'algorithm': round(1 - weight, 6), 'model': weight})
        peers: dict[tuple, dict[str, float]] = {}
        for evaluation in evaluations:
            for c in evaluation['candidates']:
                peers.setdefault((c['market'], c['line'], c['outcome']), {})[evaluation['algorithm']] = c['p_model']
        for c in out['candidates']:
            history = visible_history({**c, 'input_cutoff_ms': cutoff})
            trend = (history[-1][1] / history[0][1] - 1) * 100 if len(history) > 1 else None
            x = features({**c, 'p_fused': c['p_model'], 'entry_score': snapshot['score'],
                          'entry_clock_s': clock_seconds(
                              snapshot['status'].get('mst'), str(snapshot['status'].get('mmp') or '')),
                          'training_history': history, 'input_cutoff_ms': cutoff,
                          'feature_context': verified_context(snapshot), 'training_trend_pct': trend,
                          'algorithm_probabilities': peers.get((c['market'], c['line'], c['outcome']), {})})
            calibrated_x = calibrate_peer_vector(x, model.get('peer_calibration'))
            p = calibrate_probability(predict(model['weights'], calibrated_x), model.get('calibration'))
            c['baseline_p_model'] = float(c['p_model'])
            c['algorithm_probability'] = float(c['p_model'])
            c['p_model'] = round(p, 6)
            c['decision_model_version'] = model['version']
        # Cohere the learned outcomes first, then blend with the already
        # coherent economic ensemble. This makes the documented formula exact.
        cohere_candidates(out['candidates'])
        for c in out['candidates']:
            c['learned_probability'] = float(c['p_model'])
            c['p_model'] = round((1 - weight) * c['algorithm_probability'] +
                                 weight * c['learned_probability'], 6)
            c['decision_mode'] = mode
            c['blend_weights'] = dict(application['weights'])
            c['input_cutoff_ms'] = snapshot['captured_at_ms']
            c['decision_evidence'] = {
                'mode': mode, 'algorithm_probability': c['algorithm_probability'],
                'model_probability': c['learned_probability'],
                'weights': dict(application['weights']), 'final_probability': c['p_model'],
                'model_version': model['version'], 'input_cutoff_ms': snapshot['captured_at_ms'],
            }
            nonpush = float(c.get('win_stake') or 0) + float(c.get('loss_stake') or 0)
            if nonpush <= 0:
                continue
            ev = (float(c['p_model']) * float(c['odds']) - 1) * nonpush
            fill = q_fill_model(ev, 1.0)
            c['ev'] = round(ev, 6)
            c['q_fill'] = round(fill, 6)
            c['effective_ev'] = economics_effective_ev(ev, fill, cfg.execution_cost)
            c['kelly'] = min(cfg.max_total_exposure, max(0, fractional_kelly(c['p_model'], c['odds'], cfg.fractional_kelly)))
        groups: dict[tuple, list] = {}
        for c in out['candidates']:
            groups.setdefault((c['market'], c['line']), []).append(c)
        out['forecasts'] = [max(cs, key=lambda c: c['p_model']) for cs in groups.values()]
        picks = []
        for cs in groups.values():
            eligible = [c for c in cs if not c['research_only'] and c['p_model'] >= cfg.min_probability
                        and c['effective_ev'] >= cfg.min_ev and c['tail'] < .001 and c['kelly'] > 0]
            if eligible:
                picks.append(dict(max(eligible, key=lambda c: c['effective_ev'])))
        total = sum(c['kelly'] for c in picks)
        scale = min(1, cfg.max_total_exposure / total) if total else 0
        for c in picks:
            c['kelly'] = math.floor(c['kelly'] * scale * 1e6) / 1e6
        out['recommendations'] = [c for c in picks if c['kelly'] > 0]
        out['total_exposure'] = sum(c['kelly'] for c in out['recommendations'])
        recommended = {(c['market'], c['line'], c['outcome']) for c in out['recommendations']}
        for collection in (out['candidates'], out['recommendations']):
            for c in collection:
                c['decision_evidence']['conclusion'] = (
                    'recommend' if (c['market'], c['line'], c['outcome']) in recommended else 'forecast')
        application.update(applied=True, execution_enabled=True)
        return out
    except (OSError, ValueError, TypeError, KeyError, ArithmeticError) as exc:
        fallback = deepcopy(dict(ensemble))
        fallback['model_application'] = {
            **application, 'applied': False, 'available': False, 'state': 'unavailable',
            'execution_enabled': False, 'reason': str(exc),
        }
        fallback['candidates'] = []
        fallback['forecasts'] = []
        fallback['recommendations'] = []
        fallback['total_exposure'] = 0
        return fallback
