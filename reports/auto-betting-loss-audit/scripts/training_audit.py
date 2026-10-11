#!/usr/bin/env python3
"""Read-only, streaming audit of a frozen training projection and model artifact.

Original features/settlement are loaded from the recorded Git commit. Re-scoring
keeps those features deliberately: it assesses target contamination, not a clean
retrained model. Exact unclipped goal differences suffice for AH label recovery.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

REPO = Path(__file__).resolve().parents[3]
REPORT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from core.settlement import settle_with_basis

BASELINE_COMMIT = '4ca1897'
SCORE_DIFF_SCALE = 5
RISK_LOGIT_INDEX = 27
HIGH_RISK_LOGIT = 4.5
HALF_WEIGHT = .5
VALID = {'won', 'lost', 'half_won', 'half_lost'}


def original_module(path, dependencies=None):
    source = subprocess.check_output(['git','show',BASELINE_COMMIT+':'+path],cwd=REPO,text=True)
    if dependencies:
        source = source.replace('    from core.settlement import split_line\n','')
    namespace = {'__name__':'core.audit_original','__package__':'core', **(dependencies or {})}
    exec(compile(source,path,'exec'),namespace)
    return namespace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backup',type=Path,required=True)
    args = parser.parse_args()
    old_settle = original_module('core/settlement.py')
    old = original_module('core/learned_model.py',{'split_line':old_settle['split_line']})
    model = json.loads((args.backup/'models/active.json').read_text())
    cohort = set(model['cohort_ids'])
    validation_ids = set(model['cohort_ids'][int(len(model['cohort_ids'])*.8):])
    con = sqlite3.connect((args.backup/'ledger/ledger.sqlite3').as_uri()+'?mode=ro',uri=True)
    con.row_factory = sqlite3.Row
    finals = {}
    for raw in con.execute("SELECT payload FROM decisions WHERE competition_type='real' "
            "AND status IN ('won','lost','half_won','half_lost','push') ORDER BY settled_at"):
        row = json.loads(raw['payload'])
        if row.get('ft_score') and row.get('settled_at'):
            finals[str(row['match_id'])] = row
    scores = {mid:{'ft':r['ft_score'],'ht':r.get('ht_score'),'done':True} for mid,r in finals.items()}
    score_path = args.backup/'confirmed-scores.json'
    score_path.write_text(json.dumps({'scores':scores},ensure_ascii=False)); score_path.chmod(0o600)
    counts = Counter()
    groups = {}
    seen = set()
    validation_original = []
    validation_corrected = []
    validation_original_common = []
    training_counts = Counter()

    def record(r, x, old_status, new_status, source):
        counts[source+'_graded'] += 1
        group = groups.setdefault((source,r['market']),Counter())
        group['graded'] += 1
        group['changed_status'] += old_status != new_status
        counts[source+'_changed_status'] += old_status != new_status
        if new_status in VALID:
            counts[source+'_label_comparable'] += 1
            changed = (old_status in ('won','half_won')) != (new_status in ('won','half_won'))
            counts[source+'_flipped_label'] += changed
            group['flipped_label'] += changed
        else:
            counts[source+'_new_excluded'] += 1
            group['new_excluded'] += 1
        if (r['match_id'] not in cohort or r['at'] > model['validation_to']
                or r['settled_at'] > model['updated_at']):
            return
        item = {'match_id':r['match_id'],'x':x,'baseline':r['baseline'],
                'y':int(old_status in ('won','half_won'))}
        if r['match_id'] not in validation_ids:
            if r['settled_at'] <= model['training_cutoff']:
                training_counts['decisions'] += 1
                training_counts['changed_status'] += old_status != new_status
                training_counts['flipped_label'] += (new_status in VALID and
                      item['y'] != int(new_status in ('won','half_won')))
            return
        validation_original.append(item)
        if new_status in VALID:
            validation_original_common.append(item)
            validation_corrected.append({**item,'y':int(new_status in ('won','half_won')),
                                       'observation_weight':HALF_WEIGHT if new_status.startswith('half_') else 1})

    with sqlite3.connect((args.backup/'models/inputs.sqlite3').as_uri()+'?mode=ro',uri=True) as inputs:
        inputs.row_factory = sqlite3.Row
        for raw in inputs.execute('SELECT * FROM inputs ORDER BY at'):
            r = dict(raw)
            counts['input_total'] += 1
            x = json.loads(r['x'])
            counts['input_bad_peer_probability'] += x[RISK_LOGIT_INDEX] > HIGH_RISK_LOGIT
            final = finals.get(r['match_id'])
            if not final or final['settled_at'] <= r['at']:
                continue
            status = old_settle['settle_pick'](r['market'],r['outcome'],r['line'],final['ft_score'],final.get('ht_score'))[0]
            if status not in VALID:
                continue
            seen.add(tuple(str(r[k]) for k in ('match_id','at','algorithm','market','line','outcome')))
            r['settled_at'] = final['settled_at']
            if r['market'].startswith('AH'):
                diff = x[4] * SCORE_DIFF_SCALE
                if abs(diff)>=SCORE_DIFF_SCALE or abs(diff-round(diff))>1e-8:
                    new = 'unrecoverable_entry_difference'
                    counts['input_clipped_score_unrecoverable'] += 1
                else:
                    # AH only needs goal difference: reduce final score to that
                    # difference minus entry difference, without inventing goals.
                    result = final.get('ht_score') if r['market'].endswith('_1H') else final['ft_score']
                    if result is None:
                        new = 'void'
                    else:
                        remaining_diff = result[0]-result[1]-round(diff)
                        pseudo = (max(remaining_diff,0),max(-remaining_diff,0))
                        new = settle_with_basis(r['market'],r['outcome'],r['line'],pseudo,pseudo,
                                                settlement_basis='remaining_score',entry_score=(0,0))[0]
            else:
                new = settle_with_basis(r['market'],r['outcome'],r['line'],final['ft_score'],final.get('ht_score'),
                                        settlement_basis='half_score_required' if r['market'].endswith('_1H') else 'full_score')[0]
            record(r,x,status,new,'input')
    for raw in con.execute("SELECT payload FROM decisions WHERE competition_type='real' "
            "AND trigger IN ('live_forecast','live_recommendation') AND research_only=0 "
            "AND status IN ('won','lost','half_won','half_lost') ORDER BY at,identity"):
        r=json.loads(raw['payload'])
        key=tuple(str(r.get(k,'')) for k in ('match_id','at','algorithm','market','line','outcome'))
        if key in seen or not r.get('ft_score') or not r.get('settled_at') or r['settled_at']<=r['at']:
            continue
        try:
            x=old['features'](r)
        except (ValueError,TypeError,KeyError):
            continue
        basis = 'remaining_score' if r['market'].startswith('AH') else 'half_score_required' if r['market'].endswith('_1H') else 'full_score'
        new=settle_with_basis(r['market'],r['outcome'],r['line'],r['ft_score'],r.get('ht_score'),
                              settlement_basis=basis,entry_score=r.get('entry_score'))[0]
        r['baseline']=r['p_market']
        record(r,x,r['status'],new,'ledger')
        seen.add(key)
    con.close()
    from core.learned_model import metrics as paid_metrics
    evaluation = {'original_reconstruction':old['metrics'](validation_original,model['weights']),
                  'original_common':old['metrics'](validation_original_common,model['weights']),
                  'corrected_targets_same_features':old['metrics'](validation_corrected,model['weights']),
                  'corrected_baseline':old['metrics'](validation_corrected,None),
                  'corrected_paid_stake_weight':paid_metrics(validation_corrected,model['weights'])}
    result = {'baseline_commit':BASELINE_COMMIT,'model_version':model['version'],
              'counts':dict(counts),'training_cohort':dict(training_counts),'validation':evaluation,
              'artifact_validation':model['validation'],
              'original_decisions_match_artifact':len(validation_original)==model['validation']['decisions'],
              'limitation':'Re-scoring retains contaminated features; it is not a clean backtest or retraining. Clipped input score differences cannot be recovered.'}
    (REPORT/'data/training_contamination.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    with (REPORT/'data/training_contamination.csv').open('w',newline='') as target:
        w=csv.DictWriter(target,fieldnames=['source','market','graded','changed_status','flipped_label','new_excluded']);w.writeheader()
        for (source,market), values in sorted(groups.items()):
            w.writerow({'source':source,'market':market,**{k:values[k] for k in w.fieldnames[2:]}})
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
