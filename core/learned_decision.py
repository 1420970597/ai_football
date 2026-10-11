"""Apply compatible calibration in the isolated live calculator."""
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
    if not compatible_model(model):
        raise ValueError('训练数据版本不兼容')
    return model


def calibrate(ensemble: Mapping[str, Any], evaluations: list[dict[str, Any]],
              snapshot: Mapping[str, Any], cfg: Any, model_version: str) -> dict[str, Any]:
    out = deepcopy(dict(ensemble))
    out['model_application'] = {'mode': 'active', 'applied': False}
    try:
        path = Path(snapshot.get('_model_path') or '')
        stat = path.stat()
        model = _load(str(path), (stat.st_mtime_ns, stat.st_ino, stat.st_size))
        cutoff = snapshot['captured_at_ms']
        trained_ms = datetime.fromisoformat(model['updated_at']).timestamp() * 1000
        if trained_ms > cutoff:
            raise ValueError('模型尚未在输入截点前可用')
        if model.get('feature_version') != FEATURE_VERSION:
            raise ValueError('模型特征版本不兼容')
        promotion = model.get('promotion')
        if promotion is not None:
            if promotion.get('approved') is not True:
                raise ValueError('模型尚未通过独立晋级门槛')
        elif model['validation']['brier'] >= model['baseline']['brier']:
            raise ValueError('时间隔离验证未优于盘口基准')
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
            c['baseline_p_model'] = c['p_model']
            c['p_model'] = round(p, 6)
            c['decision_model_version'] = model['version']
            nonpush = c['win_stake'] + c['loss_stake']
            c['ev'] = round((p * c['odds'] - 1) * nonpush, 6)
            c['q_fill'] = q_fill_model(c['ev'], 1.0)
            c['effective_ev'] = economics_effective_ev(c['ev'], c['q_fill'], cfg.execution_cost)
            c['kelly'] = min(cfg.max_total_exposure, max(0, fractional_kelly(p, c['odds'], cfg.fractional_kelly)))
        cohere_candidates(out['candidates'])
        for c in out['candidates']:
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
        out['model_application'].update(applied=True, version=model['version'], model_version=model_version)
        return out
    except (OSError, ValueError, TypeError, KeyError, ArithmeticError) as exc:
        fallback = deepcopy(dict(ensemble))
        fallback['model_application'] = {'mode': 'active', 'applied': False, 'reason': str(exc)}
        return fallback
