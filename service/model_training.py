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

from core.learned_model import (DATA_VERSION, VALIDATION_EPOCHS, approved_model,
                                compatible_model, fit_weights, train)
from core.decision_features import FEATURE_VERSION
from core.model_validation import (VALIDATION_CONTRACT_VERSION, evaluate, execution_evidence,
                                   instant, is_graded_row, paired_evidence, promotion, rolling_folds)
from core.probability_calibration import fit_logistic
from core.probability_calibration import calibrate_probability
from core.oof_peer import apply_peer_calibration, calibrate_peer_vector, crossfit_peer_features
from store.training_data import dataset, training_cohort_progress

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
                         if is_graded_row(row) and instant(row['settled_at']) < test_cutoff]
    calibration_probs = [p for _, p in calibration_pairs]
    calibration_outcomes = [int(row['y']) for row, _ in calibration_pairs]
    calibration = fit_logistic(calibration_probs, calibration_outcomes) if calibration_probs else {
        'method': 'identity', 'samples': 0, 'reason': '无独立校准样本'}
    calibration.update(label_availability_cutoff=min(final_test, key=lambda row: instant(row['at']))['at'],
                       excluded_unavailable_labels=sum(
                           is_graded_row(row) and instant(row['settled_at']) >= test_cutoff
                           for earlier_rows in fold_rows[:-1] for row in earlier_rows),
                       excluded_context_rows=sum(not is_graded_row(row)
                           for earlier_rows in fold_rows[:-1] for row in earlier_rows),
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
            or not approved_model(previous)):
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
        if not approved_model(model):
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


def _artifact(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _completed_cohort(model_root: Path, artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    """Training completion, independent of whether a candidate was promoted.

    Existing successful artifacts provide a one-time migration of the water
    mark. Only the latest completed compatible generation seeds progress;
    an older active model cannot hide a newer unapproved candidate.
    """
    completed = _artifact(model_root / 'completed-training.json')
    compatible = [model for model in artifacts if compatible_model(model)]
    completed_valid = (completed.get('data_version') == DATA_VERSION
            and completed.get('feature_version') == FEATURE_VERSION
            and completed.get('validation_contract_version') == VALIDATION_CONTRACT_VERSION
            and isinstance(completed.get('cohort_ids'), list)
            and any(model.get('version') == completed.get('candidate_version') for model in compatible))
    if not compatible:
        return {}
    latest = max(compatible, key=lambda model: (str(model.get('updated_at') or ''),
                                               str(model.get('version') or '')))
    if completed_valid and str(completed.get('completed_at') or '') >= str(latest.get('updated_at') or ''):
        return completed
    # A crash may occur after candidate commit and before watermark commit.
    # Its embedded consumed cohort recovers that completion without refitting.
    consumed = set(completed.get('cohort_ids', [])) if completed_valid else set()
    consumed.update(latest.get('completed_cohort_ids') or latest.get('cohort_ids') or [])
    return {'cohort_ids': sorted(str(mid) for mid in consumed),
            'candidate_version': latest.get('version'), 'completed_at': latest.get('updated_at'),
            'source': 'compatible_artifact_recovery'}


def run(root: Path, cycle: int, cpu: int, memory: int, *, check_only: bool = False) -> dict[str, Any]:
    limits = constrain(cpu, memory)
    model_root = root.parent / 'models'
    active = model_root / 'active.json'
    active_model = _artifact(active)
    incompatible = bool(active.exists() and not approved_model(active_model))
    if incompatible and active_model:
        # Preserve both source and quarantine evidence. Loading contracts,
        # rather than deleting the artifact, block legacy execution.
        quarantine = model_root / ('quarantined-' + active.name)
        if not quarantine.exists() or _artifact(quarantine) != active_model:
            atomic_json(quarantine, active_model)
    old: dict[str, Any] = {}
    for candidate in sorted(model_root.glob('candidate-*.json'), reverse=True):
        artifact = _artifact(candidate)
        if compatible_model(artifact):
            old = artifact
            break
        incompatible = True
        if artifact and not (model_root / ('quarantined-' + candidate.name)).exists():
            atomic_json(model_root / ('quarantined-' + candidate.name), artifact)
    completed = _completed_cohort(model_root, [active_model, old])
    consumed = {str(mid) for mid in completed.get('cohort_ids', [])}
    print('training: checking qualified match cohort', flush=True)
    progress = training_cohort_progress(root / 'ledger.sqlite3', model_root / 'inputs.sqlite3',
                                        consumed_match_ids=consumed)
    cohort_ids = {str(mid) for mid in progress.get('match_ids', [])}
    frozen_labels = progress.get('qualified_labels')
    cohort_verified = (progress.get('cohort_verified') is True and isinstance(frozen_labels, dict)
                       and cohort_ids <= {str(mid) for mid in frozen_labels})
    new_count = len(cohort_ids - consumed) if cohort_verified else None
    quality = {key: value for key, value in progress.items()
               if key not in ('match_ids', 'qualified_labels')}
    status: dict[str, Any] = {'state': 'waiting', 'checked_at': datetime.now(timezone.utc).isoformat(),
                             'eligible_matches': len(cohort_ids) if cohort_verified else progress.get('eligible_matches'),
                             'eligible_decisions': progress.get('eligible_decisions'),
                             'sampled_eligible_decisions': None,
                             'eligible_decisions_scope': progress.get('counts_scope'),
                             'eligible_matches_scope': progress.get('counts_scope'),
                             'training_decisions': None, 'new_matches': new_count, 'cycle_matches': cycle,
                             'cohort_verified': cohort_verified, 'check_only': check_only,
                             'last_completed_version': completed.get('candidate_version'),
                             'completed_matches': len(consumed),
                             'cycle_ready': cohort_verified and new_count is not None and new_count >= cycle and len(cohort_ids) >= 100,
                             'limits': limits, 'minimum_matches': 100, 'error': ''}
    status.update(data_version=DATA_VERSION, feature_version=FEATURE_VERSION,
                  validation_contract_version=VALIDATION_CONTRACT_VERSION, data_quality=quality,
                  old_model_quarantined=incompatible or any(model_root.glob('quarantined-*.json')))
    if approved_model(active_model):
        status.update(state='ready', active_version=active_model.get('version'))
    if old.get('version') and old.get('promotion'):
        status.update(state='ready' if approved_model(active_model) and active_model.get('version') == old.get('version') else 'candidate',
                      candidate_version=old['version'],
                      promotion_reasons=old['promotion'].get('reasons', []),
                      candidate={'version': old['version'], 'promotion': old['promotion'],
                                'parameter_count': old.get('parameter_count'), 'architecture': old.get('architecture'),
                                'validation': old.get('validation'), 'baseline': old.get('baseline'),
                                'updated_at': old.get('updated_at'),
                                'calibration': old.get('calibration'),
                                'rolling_validation': old.get('rolling_validation', [])})
    model: dict[str, Any] = {}
    if not check_only and status['cycle_ready']:
        print('training: loading time-isolated dataset', flush=True)
        quality = {}
        cutoff = progress.get('label_cutoff') or progress.get('checked_at') or status['checked_at']
        rows = dataset(root / 'ledger.sqlite3', model_root / 'inputs.sqlite3', audit=quality,
                       max_per_match=TRAINING_MAX_ROWS_PER_MATCH,
                       cohort_labels=frozen_labels, input_cutoff=str(cutoff))
        # Freeze the cohort at the cheap probe boundary. Even when the loader
        # transaction starts later, newly arrived matches/labels cannot be
        # silently consumed by this training round.
        boundary = instant(str(cutoff))
        rows = [row for row in rows if str(row['match_id']) in cohort_ids
                and instant(row['settled_at']) <= boundary and instant(row['at']) < boundary]
        status['cohort_cutoff'] = cutoff
        graded_ids = {str(row['match_id']) for row in rows if is_graded_row(row)}
        status.update(training_decisions=len(rows), eligible_matches=len(graded_ids),
                      eligible_decisions=quality.get('valid_training_decisions'),
                      sampled_eligible_decisions=quality.get('sample_valid_training_decisions'),
                      eligible_decisions_scope=quality.get('counts_scope', 'full_inputs'),
                      eligible_matches_scope=quality.get('counts_scope', 'full_inputs'),
                      new_matches=len(graded_ids - consumed), data_quality=quality)
        if status['new_matches'] < cycle or len(graded_ids) < 100:
            status['cycle_ready'] = False
            status['cycle_reason'] = '完整曲线采样后有效比赛尚未达到训练门槛'
            atomic_json(model_root / 'status.json', status)
            return status
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
        completed_ids = consumed | graded_ids
        model['completed_cohort_ids'] = sorted(completed_ids)
        model['cohort_cutoff'] = cutoff
        candidate_path = model_root / ('candidate-' + model['version'] + '.json')
        atomic_json(candidate_path, model)
        # Advance only after a complete, durable candidate exists. The data
        # snapshot consumed here excludes inputs arriving during fitting;
        # they remain in the projection and are counted on the next check.
        atomic_json(model_root / 'completed-training.json', {
            'data_version': DATA_VERSION, 'feature_version': FEATURE_VERSION,
            'validation_contract_version': VALIDATION_CONTRACT_VERSION,
            'candidate_version': model['version'], 'completed_at': model['updated_at'],
            'cohort_ids': sorted(completed_ids), 'training_decisions': len(rows),
            'approved': bool(decision.get('approved'))})
        status.update(last_completed_version=model['version'], completed_matches=len(completed_ids),
                      new_matches=None, pending_cohort_verified=False, cycle_ready=False)
        status['candidate'] = {'version': model['version'], 'promotion': decision,
                               'parameter_count': model.get('parameter_count'), 'architecture': model.get('architecture'),
                               'validation': model.get('validation'), 'baseline': model.get('baseline'),
                               'updated_at': model.get('updated_at'),
                               'calibration': calibration, 'rolling_validation': folds}
        if approved_model(model):
            previous_backup = backup_active_model(model_root, active)
            atomic_json(active, model)
            status.update(state='ready', promoted_version=model['version'],
                          previous_active_backup=str(previous_backup) if previous_backup else None)
        else:
            status.update(state='candidate', candidate_version=model['version'],
                          promotion_reasons=decision['reasons'])
        try:
            remaining = training_cohort_progress(root / 'ledger.sqlite3', model_root / 'inputs.sqlite3',
                                                consumed_match_ids=completed_ids)
            status.update(new_matches=(len({str(mid) for mid in remaining.get('match_ids', [])} - completed_ids)
                          if remaining.get('cohort_verified') is True else None),
                          pending_cohort_verified=remaining.get('cohort_verified') is True,
                          progress_checked_at=datetime.now(timezone.utc).isoformat())
        except Exception as exc:
            # Completion stays durable even if a later progress read fails.
            status['progress_error'] = str(exc)
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
