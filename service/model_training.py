"""One constrained training worker; reads the indexed projection, never replay archives."""
from __future__ import annotations

import json
import fcntl
import os
from pathlib import Path
import resource
import shutil
import sys
from datetime import datetime, timezone
from typing import Any, Mapping

from core.learned_model import (DATA_VERSION, VALIDATION_EPOCHS, compatible_model,
                                fit_weights, train)
from core.decision_features import FEATURE_VERSION
from core.model_validation import (evaluate, execution_evidence, instant, paired_evidence,
                                   promotion, rolling_folds)
from core.probability_calibration import fit_logistic
from core.probability_calibration import calibrate_probability
from core.oof_peer import apply_peer_calibration, calibrate_peer_vector, crossfit_peer_features
from store.training_data import dataset

# Quotes arrive repeatedly for the same match, line and direction, so adjacent
# training rows are highly correlated. Keep the complete qualified-input and
# validity counts, while giving each match a deterministic, multi-decision cap
# for fitting and validation.
# A match produces many adjacent quote observations. Keep enough decisions to
# represent multiple markets/algorithms while bounding the pure-Python learner
# and every rolling-validation fold.
TRAINING_MAX_ROWS_PER_MATCH: int | None = 40
TRAINING_CPU_TIME_LIMIT_S = int(os.environ.get('MODEL_TRAINING_CPU_TIME_LIMIT_S', '1800'))


def rolling_validation(rows: list[dict[str, Any]], *,
                       final_model: Mapping[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Evaluate the residual model on chronological, match-disjoint folds."""
    fold_reports: list[dict[str, Any]] = []
    fold_rows: list[list[dict[str, Any]]] = []
    fold_candidates: list[list[float]] = []
    fold_baselines: list[list[float]] = []
    partitions = rolling_folds(rows)
    if not partitions:
        raise ValueError('无可用的时间隔离验证分区')
    final_model = final_model or train(rows, partition=partitions[-1])
    final_training, final_test = partitions[-1]
    if (set(final_model.get('training_match_ids', [])) != {str(r['match_id']) for r in final_training}
            or set(final_model.get('validation_match_ids', [])) != {str(r['match_id']) for r in final_test}):
        raise ValueError('候选模型与最终测试分区不一致')
    for index, (training, validation) in enumerate(partitions, 1):
        if index == len(partitions):
            # The independent final fold evaluates the exact weights and peer
            # calibration that will be persisted. Earlier folds may use their
            # smaller budget, but can never certify a different final model.
            weights = list(final_model['weights'])
            validation_peers = [dict(row, x=calibrate_peer_vector(
                row['x'], final_model.get('peer_calibration'))) for row in validation]
            fitted_epochs = final_model['training_epochs']
        else:
            training_oof = crossfit_peer_features(training)
            validation_peers = apply_peer_calibration(training, validation)
            weights = fit_weights(training_oof, epochs=VALIDATION_EPOCHS)
            fitted_epochs = VALIDATION_EPOCHS
        candidate = [train_probability(row, weights) for row in validation_peers]
        baseline = [float(row['baseline']) for row in validation]
        candidate_metrics = evaluate(validation_peers, candidate)
        baseline_metrics = evaluate(validation, baseline)
        fold_rows.append([dict(row) for row in validation_peers])
        fold_candidates.append(candidate)
        fold_baselines.append(baseline)
        fold_reports.append({'fold': index, 'training_matches': len({r['match_id'] for r in training}),
                             'validation_matches': len({r['match_id'] for r in validation}),
                             'validation_from': min(r['at'] for r in validation),
                             'validation_to': max(r['at'] for r in validation),
                             'model': candidate_metrics, 'baseline': baseline_metrics,
                             'delta_brier': candidate_metrics['brier'] - baseline_metrics['brier'],
                             'training_epochs': fitted_epochs,
                             'evaluates_persisted_parameters': index == len(partitions)})
    test_cutoff = min(instant(row['at']) for row in final_test)
    calibration_pairs = [(row, probability)
                         for earlier_rows, values in zip(fold_rows[:-1], fold_candidates[:-1])
                         for row, probability in zip(earlier_rows, values)
                         if instant(row['settled_at']) < test_cutoff]
    calibration_probs = [p for _, p in calibration_pairs]
    calibration_outcomes = [int(row['y']) for row, _ in calibration_pairs]
    calibration = fit_logistic(calibration_probs, calibration_outcomes) if calibration_probs else {
        'method': 'identity', 'samples': 0, 'reason': '无独立校准样本'}
    calibration.update(label_availability_cutoff=min(final_test, key=lambda row: instant(row['at']))['at'],
                       excluded_unavailable_labels=sum(len(values) for values in fold_rows[:-1])-len(calibration_pairs),
                       matches=len({str(row['match_id']) for row, _ in calibration_pairs}))
    test_rows = fold_rows[-1] if fold_rows else []
    test_candidate = [calibrate_probability(p, calibration) for p in (fold_candidates[-1] if fold_candidates else [])]
    test_baseline = fold_baselines[-1] if fold_baselines else []
    evidence = paired_evidence(test_rows, test_candidate, test_baseline) if test_rows else {
        'matches': 0, 'delta_brier': 0, 'ci95': [0, 0], 'method': 'paired match-cluster bootstrap'}
    evidence['execution_cohort'] = execution_evidence(test_rows, test_candidate, test_baseline)
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


def backup_active_model(model_root: Path, active: Path | None = None) -> Path | None:
    """Preserve the current active artifact before a promotion.

    Backups are immutable, version-addressable artifacts.  An invalid or
    missing active model is deliberately ignored; promotion can still create
    the first active artifact.  The copy is made before ``active.json`` is
    replaced so an interrupted promotion never destroys the last known-good
    model.
    """
    active = active or (model_root / 'active.json')
    if not active.exists():
        return None
    try:
        previous = json.loads(active.read_text(encoding='utf-8'))
        version = str(previous.get('version') or '')
    except (OSError, ValueError, TypeError):
        return None
    if (not version or any(ch not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-' for ch in version)
            or not compatible_model(previous)):
        return None
    destination = model_root / ('active-backup-' + version + '.json')
    if not destination.exists():
        model_root.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix('.tmp')
        shutil.copyfile(active, temporary)
        os.replace(temporary, destination)
    return destination


def rollback_active_model(model_root: Path, version: str | None = None) -> dict[str, Any]:
    """Explicitly restore a previously approved active backup.

    This function is intentionally never called by the worker automatically.
    Only artifacts carrying a compatible data/feature version and an explicit
    ``promotion.approved`` flag are eligible, preventing accidental downgrade
    to an unvalidated candidate.
    """
    backups = sorted(model_root.glob('active-backup-*.json'))
    if version:
        version = str(version)
        if any(ch not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-' for ch in version):
            return {'restored': False, 'reason': '无效模型版本'}
        backups = [p for p in backups if p.name == 'active-backup-' + version + '.json']
    for path in reversed(backups):
        try:
            model = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError):
            continue
        promotion = model.get('promotion')
        if not compatible_model(model) or not isinstance(promotion, dict) or promotion.get('approved') is not True:
            continue
        atomic_json(model_root / 'active.json', model)
        return {'restored': True, 'version': str(model.get('version')), 'source': str(path)}
    reason = '未找到兼容且已批准的 active 备份'
    if version:
        reason += ': ' + str(version)
    return {'restored': False, 'reason': reason}


def constrain(cpu: int, memory_mb: int) -> dict[str, Any]:
    allowed = sorted(os.sched_getaffinity(0))
    os.sched_setaffinity(0, allowed[:cpu])
    memory = memory_mb * 1024**2
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (TRAINING_CPU_TIME_LIMIT_S, TRAINING_CPU_TIME_LIMIT_S))
    os.nice(10)
    return {'cpu_cores': len(os.sched_getaffinity(0)), 'memory_mb': memory_mb,
            'cpu_time_limit_s': TRAINING_CPU_TIME_LIMIT_S}


def run(root: Path, cycle: int, cpu: int, memory: int, *, check_only: bool = False) -> dict[str, Any]:
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
        active.unlink(missing_ok=True)
        old = {}
    quality: dict[str, Any] = {}
    print('training: loading time-isolated dataset', flush=True)
    rows = dataset(root / 'ledger.sqlite3', model_root / 'inputs.sqlite3', audit=quality,
                   max_per_match=TRAINING_MAX_ROWS_PER_MATCH)
    consumed = set(old.get('cohort_ids', []))
    new_count = len({r['match_id'] for r in rows} - consumed)
    status: dict[str, Any] = {'state': 'waiting', 'checked_at': datetime.now(timezone.utc).isoformat(),
                             'eligible_matches': len({r['match_id'] for r in rows}),
                             'eligible_decisions': quality.get('valid_training_decisions'),
                             'sampled_eligible_decisions': quality.get('sample_valid_training_decisions'),
                             'eligible_decisions_scope': quality.get('counts_scope', 'full_inputs'),
                             'eligible_matches_scope': quality.get('counts_scope', 'full_inputs'),
                             'training_decisions': len(rows), 'new_matches': new_count, 'cycle_matches': cycle,
                             'limits': limits, 'minimum_matches': 100, 'error': ''}
    status.update(data_version=DATA_VERSION, feature_version=FEATURE_VERSION, data_quality=quality,
                  old_model_quarantined=incompatible or any(model_root.glob('quarantined-*.json')))
    if not active.exists() and old.get('version') and old.get('promotion'):
        status.update(state='candidate', candidate_version=old['version'],
                      promotion_reasons=old['promotion'].get('reasons', []),
                      candidate={'version': old['version'], 'promotion': old['promotion'],
                                'parameter_count': old.get('parameter_count'), 'architecture': old.get('architecture'),
                                'validation': old.get('validation'), 'baseline': old.get('baseline'),
                                'updated_at': old.get('updated_at'),
                                'calibration': old.get('calibration'),
                                'rolling_validation': old.get('rolling_validation', [])})
    model: dict[str, Any] = {}
    if not check_only and new_count >= cycle and status['eligible_matches'] >= 100:
        status['state'] = 'training'
        atomic_json(model_root / 'status.json', status)
        print('training: fitting model', flush=True)
        partitions = rolling_folds(rows)
        if not partitions:
            raise ValueError('无可用的时间隔离验证分区')
        model = train(rows, partition=partitions[-1])
        print('training: rolling validation', flush=True)
        folds, evidence, calibration = rolling_validation(rows, final_model=model)
        test_metrics = folds[-1].get('model_calibrated', model['validation']) if folds else model['validation']
        baseline_metrics = folds[-1].get('baseline_calibrated', model['baseline']) if folds else model['baseline']
        decision = promotion(test_metrics, baseline_metrics, evidence, folds)
        model['validation_uncalibrated'] = model['validation']
        model['validation'] = test_metrics
        model['baseline'] = baseline_metrics
        model['validation_basis'] = 'persisted artifact; chronological final fold; calibrated and coherent'
        model['sampling'] = {'method':quality.get('sampling_method', 'unknown'),
                             'max_rows_per_match':TRAINING_MAX_ROWS_PER_MATCH,
                             'cap_basis':quality.get('sampling_cap_basis'),
                             'row_bound':quality.get('sampling_row_bound'),
                             'source_eligible_decisions':quality.get('source_eligible_decisions'),
                             'source_eligible_decisions_scope':quality.get('counts_scope', 'full_inputs'),
                             'sampled_eligible_decisions':quality.get('sample_valid_training_decisions')}
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
                               'parameter_count': model.get('parameter_count'), 'architecture': model.get('architecture'),
                               'validation': model.get('validation'), 'baseline': model.get('baseline'),
                               'updated_at': model.get('updated_at'),
                               'calibration': calibration, 'rolling_validation': folds}
        if decision['approved']:
            previous_backup = backup_active_model(model_root, active)
            atomic_json(active, model)
            status.update(state='ready', new_matches=0, promoted_version=model['version'],
                          previous_active_backup=str(previous_backup) if previous_backup else None)
        else:
            status.update(state='candidate', candidate_version=model['version'],
                          promotion_reasons=decision['reasons'])
    # Independent observation loop grades persisted predictions. Training
    # must not overwrite its versioned prospective window on completion.
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
            run(root, int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]),
                check_only=len(sys.argv) > 5 and sys.argv[5] == '--check')
        except Exception as exc:
            path = model_root / 'status.json'
            status = json.loads(path.read_text()) if path.exists() else {}
            atomic_json(path, {**status, 'state': 'failed', 'error': str(exc),
                              'checked_at': datetime.now(timezone.utc).isoformat()})
            raise
