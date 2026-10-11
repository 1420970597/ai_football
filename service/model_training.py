"""One constrained training worker; reads the indexed projection, never replay archives."""
from __future__ import annotations

import json
import fcntl
import os
from pathlib import Path
import resource
import sys
from datetime import datetime, timezone
from typing import Any, Mapping

from core.learned_model import DATA_VERSION, compatible_model, fit_weights, train
from core.decision_features import FEATURE_VERSION
from core.model_validation import evaluate, paired_evidence, promotion, rolling_folds
from core.probability_calibration import fit_logistic
from core.probability_calibration import calibrate_probability
from store.training_data import dataset

TRAINING_MAX_ROWS_PER_MATCH = 64


def rolling_validation(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Evaluate the residual model on chronological, match-disjoint folds."""
    fold_reports: list[dict[str, Any]] = []
    fold_rows: list[list[dict[str, Any]]] = []
    fold_candidates: list[list[float]] = []
    fold_baselines: list[list[float]] = []
    fold_outcomes: list[list[int]] = []
    for index, (training, validation) in enumerate(rolling_folds(rows), 1):
        weights = fit_weights(training)
        candidate = [train_probability(row, weights) for row in validation]
        baseline = [float(row['baseline']) for row in validation]
        candidate_metrics = evaluate(validation, candidate)
        baseline_metrics = evaluate(validation, baseline)
        fold_rows.append([dict(row) for row in validation])
        fold_candidates.append(candidate)
        fold_baselines.append(baseline)
        fold_outcomes.append([int(row['y']) for row in validation])
        fold_reports.append({'fold': index, 'training_matches': len({r['match_id'] for r in training}),
                             'validation_matches': len({r['match_id'] for r in validation}),
                             'validation_from': min(r['at'] for r in validation),
                             'validation_to': max(r['at'] for r in validation),
                             'model': candidate_metrics, 'baseline': baseline_metrics,
                             'delta_brier': candidate_metrics['brier'] - baseline_metrics['brier']})
    calibration_probs = [p for values in fold_candidates[:-1] for p in values]
    calibration_outcomes = [y for values in fold_outcomes[:-1] for y in values]
    calibration = fit_logistic(calibration_probs, calibration_outcomes) if calibration_probs else {
        'method': 'identity', 'samples': 0, 'reason': '无独立校准样本'}
    test_rows = fold_rows[-1] if fold_rows else []
    test_candidate = [calibrate_probability(p, calibration) for p in (fold_candidates[-1] if fold_candidates else [])]
    test_baseline = fold_baselines[-1] if fold_baselines else []
    evidence = paired_evidence(test_rows, test_candidate, test_baseline) if test_rows else {
        'matches': 0, 'delta_brier': 0, 'ci95': [0, 0], 'method': 'paired match-cluster bootstrap'}
    if fold_reports:
        fold_reports[-1]['model_calibrated'] = evaluate(test_rows, test_candidate)
        fold_reports[-1]['baseline_calibrated'] = evaluate(test_rows, test_baseline)
    return fold_reports, evidence, calibration


def train_probability(row: Mapping[str, Any], weights: list[float]) -> float:
    from core.learned_model import predict
    return predict(weights, row['x'])


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def constrain(cpu: int, memory_mb: int) -> dict[str, Any]:
    allowed = sorted(os.sched_getaffinity(0))
    os.sched_setaffinity(0, allowed[:cpu])
    memory = memory_mb * 1024**2
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (600, 600))
    os.nice(10)
    return {'cpu_cores': len(os.sched_getaffinity(0)), 'memory_mb': memory_mb, 'cpu_time_limit_s': 600}


def run(root: Path, cycle: int, cpu: int, memory: int) -> dict[str, Any]:
    limits = constrain(cpu, memory)
    model_root = root.parent / 'models'
    active = model_root / 'active.json'
    old = json.loads(active.read_text()) if active.exists() else {}
    if not old:
        candidates = sorted(model_root.glob('candidate-*.json'))
        if candidates:
            try:
                old = json.loads(candidates[-1].read_text())
            except (OSError, ValueError, TypeError):
                old = {}
    incompatible = bool(old and not compatible_model(old))
    if incompatible:
        # Retain the artifact; it may be audited but must never seed a clean cycle.
        atomic_json(model_root / ('quarantined-' + active.name), old)
        active.unlink()
        old = {}
    quality: dict[str, Any] = {}
    rows = dataset(root / 'ledger.sqlite3', model_root / 'inputs.sqlite3', audit=quality,
                   max_per_match=TRAINING_MAX_ROWS_PER_MATCH)
    consumed = set(old.get('cohort_ids', []))
    new_count = len({r['match_id'] for r in rows} - consumed)
    status: dict[str, Any] = {'state': 'waiting', 'checked_at': datetime.now(timezone.utc).isoformat(),
                             'eligible_matches': len({r['match_id'] for r in rows}), 'eligible_decisions': len(rows), 'new_matches': new_count, 'cycle_matches': cycle,
                             'limits': limits, 'minimum_matches': 100, 'error': ''}
    status.update(data_version=DATA_VERSION, feature_version=FEATURE_VERSION, data_quality=quality,
                  old_model_quarantined=incompatible or any(model_root.glob('quarantined-*.json')))
    if not active.exists() and old.get('version') and old.get('promotion'):
        status.update(state='candidate', candidate_version=old['version'],
                      promotion_reasons=old['promotion'].get('reasons', []),
                      candidate={'version': old['version'], 'promotion': old['promotion'],
                                'calibration': old.get('calibration'),
                                'rolling_validation': old.get('rolling_validation', [])})
    model: dict[str, Any] = {}
    if new_count >= cycle and status['eligible_matches'] >= 100:
        status['state'] = 'training'
        atomic_json(model_root / 'status.json', status)
        model = train(rows)
        folds, evidence, calibration = rolling_validation(rows)
        test_metrics = folds[-1].get('model_calibrated', model['validation']) if folds else model['validation']
        baseline_metrics = folds[-1].get('baseline_calibrated', model['baseline']) if folds else model['baseline']
        decision = promotion(test_metrics, baseline_metrics, evidence, folds)
        model['sampling'] = {'method':'deterministic identity min-hash, independent of results',
                             'max_rows_per_match':TRAINING_MAX_ROWS_PER_MATCH,
                             'source_eligible_decisions':quality['source_eligible_decisions']}
        model['rolling_validation'] = folds
        model['promotion'] = decision
        model['promotion_evidence'] = evidence
        model['calibration'] = calibration
        model['feature_version'] = FEATURE_VERSION
        model['updated_at'] = datetime.now(timezone.utc).isoformat()
        model['version'] = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        model['limits'] = limits
        candidate_path = model_root / ('candidate-' + model['version'] + '.json')
        atomic_json(candidate_path, model)
        status['candidate'] = {'version': model['version'], 'promotion': decision,
                               'calibration': calibration, 'rolling_validation': folds}
        if decision['approved']:
            atomic_json(active, model)
            status.update(state='ready', new_matches=0, promoted_version=model['version'])
        else:
            status.update(state='candidate', candidate_version=model['version'],
                          promotion_reasons=decision['reasons'])
    from store.training_data import prospective_metrics
    current = (model if model and status.get('state') == 'ready' else
               (json.loads(active.read_text()) if active.exists() else {}))
    atomic_json(model_root / 'prospective.json', prospective_metrics(root / 'ledger.sqlite3', model_root / 'shadow.sqlite3', current))
    atomic_json(model_root / 'status.json', status)
    return status


if __name__ == '__main__':
    root = Path(sys.argv[1])
    model_root = root.parent / 'models'
    model_root.mkdir(parents=True, exist_ok=True)
    with (model_root / 'training.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(0)  # Another process owns training for this data directory.
        try:
            run(root, int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]))
        except Exception as exc:
            path = model_root / 'status.json'
            status = json.loads(path.read_text()) if path.exists() else {}
            atomic_json(path, {**status, 'state': 'failed', 'error': str(exc),
                              'checked_at': datetime.now(timezone.utc).isoformat()})
            raise
