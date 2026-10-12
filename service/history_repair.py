"""Rebuild derived history from frozen inputs, never certify missing evidence."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import math
from typing import Any, Mapping

from core.ensemble import adaptive_weights, pool
from core.live_model import MODEL_VERSION, valid_score
from core.settlement import pnl_for, settle_with_basis
from service.live_expert import LiveExpertService
from service.runtime_settings import ALGORITHMS, RuntimeConfig, validated

REPAIR_VERSION = 'TASK-42-frozen-input-v2'
GRADING_VERSION = 'remaining-ah-signed-lines-v2'


def merge_final_evidence(finals: Mapping[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """Score revisions follow event time, including the old unsuffixed journal."""
    out = deepcopy(dict(finals))
    journal: dict[str, Any] = {}
    for row in sorted(events,key=lambda r:datetime.fromisoformat(r['at'])):
        if row.get('done') is not True or valid_score(row.get('ft')) is None:
            continue
        mid, ft = str(row['match_id']), list(valid_score(row['ft']) or ())
        previous = journal.get(mid,{})
        ht = row.get('ht') or previous.get('ht_score')
        same = previous.get('ft_score') == ft and (not row.get('ht') or previous.get('ht_score') == ht)
        journal[mid] = {'ft_score':ft,'ht_score':ht,'settled_at':previous['settled_at'] if same else row['at'],
                        'score_evidence_at':row['at']}
    for mid,row in journal.items():
        previous = out.get(mid,{})
        same = previous.get('ft_score') == tuple(row['ft_score']) or previous.get('ft_score') == row['ft_score']
        same = same and (not row.get('ht_score') or not previous.get('ht_score') or
                        list(previous['ht_score']) == list(row['ht_score']))
        if same and previous.get('settled_at'):
            row['settled_at'] = min(previous['settled_at'],row['settled_at'],key=datetime.fromisoformat)
        out[mid] = row
    return out


def repair_grade(row: Mapping[str, Any], finals: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row)
    market = str(out.get('market', ''))
    if out.get('is_live') and market.startswith('AH'):
        out['settlement_basis'] = 'remaining_score'
    final = finals.get(str(out.get('match_id')), {})
    ft = valid_score(final.get('ft_score')) or valid_score(out.get('ft_score'))
    ht = valid_score(final.get('ht_score')) or valid_score(out.get('ht_score'))
    if ft and ht and any(h > f for h,f in zip(ht,ft)):
        ht = None
    if ft is None or (market.endswith('_1H') and ht is None):
        out['repair_status'] = 'missing_final_score'
        if out.get('status') in ('won','lost','half_won','half_lost','push'):
            out.update(original_status=out['status'], original_pnl=out.get('pnl'),
                       status='void',pnl=0,correct=None,grading_version=GRADING_VERSION,
                       settle_note='原结算缺少完整赛果证据，排除统计并等待恢复')
        return out
    status, note = settle_with_basis(market, out.get('outcome'), out.get('line'), ft, ht,
                                    settlement_basis=out.get('settlement_basis', 'unknown'), entry_score=out.get('entry_score'))
    if status == 'void':
        out['repair_status'] = 'unverified_settlement'
        out.update(original_status=out.get('status'),original_pnl=out.get('pnl'),
                   status='void',pnl=0,correct=None,grading_version=GRADING_VERSION,settle_note=note)
        return out
    if out.get('is_pick'):
        try:
            odds = float(out['odds'])
        except (ValueError,TypeError,KeyError):
            odds = 0
        if not math.isfinite(odds) or odds <= 1:
            out.update(repair_status='unverified_price',original_status=out.get('status'),
                       original_pnl=out.get('pnl'),status='void',pnl=0,correct=None,
                       grading_version=GRADING_VERSION,settle_note='原入场赔率无效，排除收益统计')
            return out
    out.update(status=status, settle_note=note, grading_version=GRADING_VERSION,
               correct=status in ('won', 'half_won'), repair_status='regraded')
    score_changed = (valid_score(row.get('ft_score')) != ft or
                     (market.endswith('_1H') and valid_score(row.get('ht_score')) != ht))
    out['ft_score'], out['ht_score'] = list(ft), list(ht) if ht is not None else None
    if score_changed and final.get('settled_at'):
        out['settled_at'] = final['settled_at']
    elif not out.get('settled_at') and final.get('settled_at'):
        out['settled_at'] = final['settled_at']
    if out.get('is_pick'):
        out['pnl'] = round(pnl_for(status, float(out['odds'])), 6)
    return out


def repair_replay(row: Mapping[str, Any], finals: Mapping[str, Any]) -> dict[str, Any]:
    """Saved goal rates and markets suffice; contaminated past weights become priors.

    RAW markets remain in immutable original packs. The revised journal links its
    source and retains all supported prices and every pre-cutoff training prefix.
    """
    out = dict(row)
    out['repair_version'] = REPAIR_VERSION
    if row.get('model_version') == MODEL_VERSION:
        out['repair_status'] = 'already_current'
        return out
    score = valid_score(row.get('score'))
    evaluations = row.get('evaluations') or []
    candidates = [c for e in evaluations for c in e.get('candidates', [])] or row.get('candidates') or []
    if not candidates:
        out['repair_status'] = 'no_derived_prediction'
        out['model_version'] = MODEL_VERSION
        out['markets'] = [m for m in row.get('markets', []) if m.get('supported')]
        return out
    algorithms = tuple(e['algorithm'] for e in evaluations if e.get('algorithm') in ALGORITHMS)
    frozen = row.get('config') or {}
    cfg = validated(RuntimeConfig(), {k: v for k, v in frozen.items()
                                     if k in RuntimeConfig.__dataclass_fields__ and k != 'llm_api_key'})
    if algorithms:
        cfg = replace(cfg, algorithms=algorithms,
                      primary_algorithm=cfg.primary_algorithm if cfg.primary_algorithm in algorithms else algorithms[0],
                      model_shadow_enabled=True)
    rates = {e['algorithm']: e.get('remaining_goals') for e in evaluations}
    current = rates.get('poisson_market')
    decayed = next((rates[a] for a in algorithms if a != 'poisson_market' and rates.get(a)), current)
    if score is None or not algorithms or not current or not decayed or not row.get('markets'):
        # Early compact logs did not save rates. AH cannot be recomputed from a
        # clipped/rounded predicted probability alone. Preserve it as evidence.
        out['repair_status'] = 'missing_frozen_rates_or_markets'
        out['training_eligible'] = False
        out['candidates'] = [dict(c) for c in candidates]
        for c in out['candidates']:
            if str(c['market']).startswith('AH'):
                c['original_p_model'] = c.pop('p_model', None)
                c['prediction_repair_status'] = 'missing_goal_distribution'
            final = finals.get(str(row['match_id']))
            if final:
                graded = repair_grade({**c, 'match_id':row['match_id'], 'entry_score':score,
                    'is_live':True, 'model_version':row.get('model_version'),
                    'settlement_basis':'remaining_score' if str(c['market']).startswith('AH') else
                       'half_score_required' if str(c['market']).endswith('_1H') else 'full_score'}, finals)
                c.update({k:graded[k] for k in ('status','grading_version','repair_status') if k in graded})
        return out
    markets = [deepcopy(m) for m in row['markets'] if m.get('supported')]
    direction = any(m['market'] == 'HAD' and m.get('fresh') and m.get('complete') for m in markets)
    performance = adaptive_weights(algorithms, {}, cfg.weight_prior_matches, cfg.max_algorithm_weight)
    rebuilt = LiveExpertService.evaluate_markets(markets, score, row.get('elapsed_s'), current, decayed,
                                                performance, cfg, direction)
    ensemble = pool(rebuilt, performance, cfg)
    cutoff = row.get('input_cutoff_ms')
    received = row.get('received_at_ms')
    time_contract = (row.get('schema_version') == 3 and
                     isinstance(cutoff, (int, float)) and not isinstance(cutoff, bool) and
                     math.isfinite(cutoff) and cutoff > 0 and
                     isinstance(received, (int, float)) and not isinstance(received, bool) and
                     math.isfinite(received) and 0 < received <= cutoff)
    out.update(model_version=MODEL_VERSION, original_model_version=row.get('model_version'),
               repair_status='rebuilt_frozen_inputs', markets=markets, evaluations=rebuilt,
               ensemble=ensemble, baseline_ensemble=ensemble, candidates=ensemble['candidates'],
               forecasts=ensemble['forecasts'], picks=ensemble['recommendations'],
               has_buy=bool(ensemble['recommendations']), algorithm_performance=performance,
               training_eligible=time_contract,
               repaired_weight_basis='neutral_prior; old settlement evidence was contaminated')
    return out
