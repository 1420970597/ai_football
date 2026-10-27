"""Separate extraction-count, binding, rejection, coverage and duplicate risks."""

from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json

from .formats import FIELDS, PROVENANCE_FIELDS, identity, validate_labels, validate_run
from .matching import align


def counters():
    return {
        'coverage': {key: 0 for key in ('samples', 'reported', 'missing', 'bound', 'input_mismatch', 'binding_missing',
                                      'completed', 'execution_error', 'status_missing', 'assessed', 'unassessed', 'unbound_accepted')},
        'extraction_count_proxy': {key: 0 for key in ('expected_recommendations', 'unique_accepted_recommendations',
            'assessed_positive_messages', 'same_count_messages', 'missing_by_count', 'extra_by_count', 'unassessed_expected')},
        'exact_binding': {key: 0 for key in ('correct', 'missed', 'false_accepted', 'unassessed_expected')},
        'dimensions': {field: {key: 0 for key in ('expected', 'correct', 'incorrect', 'missing', 'ambiguous', 'unassessed')}
                       for field in FIELDS},
        'decisions': {key: 0 for key in ('expected_accept_messages', 'expected_reject_messages', 'false_accept_messages',
            'correct_reject_messages', 'correct_reject_with_reason', 'rejection_reason_missing', 'false_reject_messages', 'unassessed_rejections')},
        'multiple_recommendations': {key: 0 for key in ('messages', 'assessed', 'all_exact', 'partial_exact', 'none_exact',
            'under_count_messages', 'missing_by_count', 'missing_exact', 'unassessed')},
        'duplicates': {'outputs': 0, 'acknowledgements': 0, 'affected_messages': 0},
    }


def add(target, source):
    for key, value in source.items():
        if isinstance(value, dict):
            add(target[key], value)
        else:
            target[key] += value


def ratio(value, total):
    return str((Decimal(value) / total).quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)) if total else None


def finish(values):
    result = deepcopy(values)
    for metric in result['dimensions'].values():
        # Missing, ambiguous and unassessed labels stay in this denominator.
        metric['correct_per_expected'] = ratio(metric['correct'], metric['expected'])
    return result


def _sample(label, output):
    expected = label['expected']['recommendations']
    count = len(expected)
    metric, flags = counters(), []
    metric['coverage']['samples'] = 1
    metric['extraction_count_proxy']['expected_recommendations'] = count
    metric['decisions']['expected_accept_messages' if count else 'expected_reject_messages'] = 1
    metric['multiple_recommendations']['messages'] = int(count > 1)
    for value in metric['dimensions'].values():
        value['expected'] = count
    source = label['source']
    bound, accepted = False, []
    if output is None:
        metric['coverage']['missing'] = 1
        flags.append('prediction_record_missing')
    else:
        metric['coverage']['reported'] = 1
        required_media = source.get('media_sha256', [])
        if (not output.get('source_content_sha256') or not isinstance(output.get('message_revision'), int)
                or isinstance(output.get('message_revision'), bool)
                or required_media and not isinstance(output.get('source_media_sha256'), list)):
            metric['coverage']['binding_missing'] = 1
            flags.append('source_hash_revision_or_media_manifest_missing')
        elif (output['source_content_sha256'] != source['content_sha256']
              or output['message_revision'] != source['message_revision']
              or output.get('source_media_sha256', []) != required_media):
            metric['coverage']['input_mismatch'] = 1
            flags.append('source_content_or_revision_mismatch')
        else:
            bound = True
            metric['coverage']['bound'] = 1
        status = output.get('status')
        metric['coverage']['completed' if status == 'completed' else 'execution_error' if status == 'error' else 'status_missing'] = 1
        accepted = output['normalized_accepted']
        metric['coverage']['unbound_accepted'] = len(accepted) if not bound else 0
        metric['duplicates'] = {'outputs': output['duplicate_outputs'], 'acknowledgements': output['duplicate_acknowledgements'],
                                'affected_messages': int(output['duplicate_outputs'] > 0 or output['duplicate_acknowledgements'] > 0)}
        flags.extend(output['missing_fields'])
        if output['duplicate_outputs']:
            flags.append('duplicate_accepted_outputs')
        if not output['accepted_supplied']:
            flags.append('accepted_list_missing')
        if status != 'completed':
            flags.append('execution_error' if status == 'error' else 'execution_status_missing')
    # Already accepted signals remain a risk even if a later framework call failed.
    assessed = bound and output['accepted_supplied'] and (output.get('status') == 'completed' or bool(accepted))
    pairs = []
    if not assessed:
        metric['coverage']['unassessed'] = 1
        metric['extraction_count_proxy']['unassessed_expected'] = count
        metric['exact_binding']['unassessed_expected'] = count
        for value in metric['dimensions'].values():
            value['unassessed'] = count
        metric['multiple_recommendations']['unassessed'] = int(count > 1)
        metric['decisions']['unassessed_rejections'] = int(count == 0)
    else:
        metric['coverage']['assessed'] = 1
        metric['extraction_count_proxy']['unique_accepted_recommendations'] = len(accepted)
        pairs = align(expected, accepted)
        exact = sum(pair['exact'] for pair in pairs)
        metric['exact_binding'].update(correct=exact, missed=count - exact, false_accepted=len(accepted) - exact)
        if count:
            metric['extraction_count_proxy'].update(assessed_positive_messages=1, same_count_messages=int(count == len(accepted)),
                missing_by_count=max(0, count - len(accepted)), extra_by_count=max(0, len(accepted) - count))
            if not accepted:
                metric['decisions']['false_reject_messages'] = 1
        elif accepted:
            metric['decisions']['false_accept_messages'] = 1
        elif output.get('status') == 'completed':
            metric['decisions']['correct_reject_messages'] = 1
            explained = isinstance(output.get('rejection_reason'), str) and bool(output['rejection_reason'].strip())
            metric['decisions']['correct_reject_with_reason'] = int(explained)
            metric['decisions']['rejection_reason_missing'] = int(not explained)
        if count > 1:
            metric['multiple_recommendations'].update(assessed=1,
                all_exact=int(exact == count and len(accepted) == count and not output['duplicate_outputs']),
                partial_exact=int(0 < exact < count), none_exact=int(exact == 0),
                under_count_messages=int(len(accepted) < count), missing_by_count=max(0, count - len(accepted)), missing_exact=count - exact)
        for pair in pairs:
            if pair['ambiguous']:
                flags.append('diagnostic_alignment_ambiguous')
            for field in FIELDS:
                dimension = metric['dimensions'][field]
                if pair['ambiguous']:
                    dimension['ambiguous'] += 1
                elif pair['actual'] is None:
                    dimension['missing'] += 1
                else:
                    observed = accepted[pair['actual']][field]
                    dimension['missing' if observed is None else 'correct' if observed == expected[pair['expected']][field] else 'incorrect'] += 1
    return {'sample_id': label['sample_id'], 'source_level': source['level'], 'source_reference': source['reference'],
            'content_sha256': source['content_sha256'], 'media_sha256': source.get('media_sha256', []),
            'message_revision': source['message_revision'],
            'modality': label['modality'], 'expected': expected, 'accepted': accepted,
            'status': output.get('status') if output else None, 'source_bound': bound, 'assessed': bool(assessed),
            'flags': sorted(set(flags)), 'alignment': pairs, 'metrics': metric}


def _aggregate(samples):
    result = counters()
    for sample in samples:
        add(result, sample['metrics'])
    return finish(result)


def evaluate(labels, predictions):
    labels = validate_labels(labels)
    predictions = validate_run(predictions, labels['dataset_id'])
    indexed = {row['sample_id']: row for row in predictions['samples']}
    samples = [_sample(label, indexed.get(label['sample_id'])) for label in labels['samples']]
    known = {row['sample_id'] for row in labels['samples']}
    orphan = [row for row in predictions['samples'] if row['sample_id'] not in known]
    provenance = {field: predictions.get('provenance', {}).get(field) for field in PROVENANCE_FIELDS}
    return {'format': 'football-review-evaluation-v1', 'dataset_id': labels['dataset_id'], 'run_id': predictions['run_id'],
            'annotation_version': labels['annotation_version'], 'synthetic_only': True,
            'real_world_accuracy_available': False, 'profitability_assessed': False,
            'label_digest_sha256': hashlib.sha256(json.dumps(labels, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest(),
            'provenance': provenance, 'provenance_complete': not predictions['provenance_missing'],
            'provenance_missing': predictions['provenance_missing'], 'metrics': _aggregate(samples),
            'orphan_predictions': [{'sample_id': row['sample_id'], 'accepted_count': len(row['normalized_accepted'])} for row in orphan],
            'groups': {'modality': {mode: _aggregate([row for row in samples if row['modality'] == mode])
                                     for mode in sorted({row['modality'] for row in samples})},
                       'phase': {phase: _aggregate([row for row in samples if sample_phase(row) == phase])
                                 for phase in ('scheduled', 'live', 'mixed', 'reject')}},
            'samples': samples,
            'interpretation': {'extraction': 'accepted_count_proxy_only_not_internal_extraction_accuracy',
                               'alignment': 'one_to_one_diagnostic_minimum_mismatch_equal_optima_are_ambiguous',
                               'dimensions': 'correct_over_all_expected_including_missing_and_unassessed',
                               'promotion': 'no_automatic_winner_or_promotion'}}


def sample_phase(row):
    phases = {item['phase'] for item in row['expected']}
    return 'reject' if not phases else next(iter(phases)) if len(phases) == 1 else 'mixed'


def _delta(before, after):
    return {key: _delta(before[key], value) if isinstance(value, dict) else value - before[key]
            for key, value in after.items() if not isinstance(value, str) and value is not None}


def compare(labels, baseline, candidate):
    before, after = evaluate(labels, baseline), evaluate(labels, candidate)
    review = []
    for old, new in zip(before['samples'], after['samples']):
        reasons = []
        if {identity(item) for item in old['accepted']} != {identity(item) for item in new['accepted']}:
            reasons.append('accepted_bindings_changed')
        for key in ('status', 'source_bound', 'assessed', 'flags'):
            if old[key] != new[key]:
                reasons.append(key + '_changed')
        if old['metrics']['duplicates'] != new['metrics']['duplicates']:
            reasons.append('duplicate_risk_changed')
        if reasons:
            review.append({'sample_id': old['sample_id'], 'source_level': old['source_level'],
                           'source_reference': old['source_reference'], 'expected': old['expected'],
                           'baseline': {key: old[key] for key in ('accepted', 'flags', 'status', 'assessed')},
                           'candidate': {key: new[key] for key in ('accepted', 'flags', 'status', 'assessed')},
                           'metric_changes': _delta(old['metrics'], new['metrics']), 'reasons': reasons,
                           'human_review_required': True})
    return {'format': 'football-review-comparison-v1', 'dataset_id': before['dataset_id'], 'synthetic_only': True,
            'annotation_version': before['annotation_version'], 'label_digest_sha256': before['label_digest_sha256'],
            'automatic_promotion': False, 'real_world_accuracy_available': False, 'profitability_assessed': False,
            'baseline_run_id': before['run_id'], 'candidate_run_id': after['run_id'],
            'baseline_provenance': before['provenance'], 'candidate_provenance': after['provenance'],
            'provenance_changed': [key for key in PROVENANCE_FIELDS if before['provenance'][key] != after['provenance'][key]],
            'provenance_missing': {'baseline': before['provenance_missing'], 'candidate': after['provenance_missing']},
            'metric_changes': _delta(before['metrics'], after['metrics']), 'baseline_metrics': before['metrics'],
            'candidate_metrics': after['metrics'], 'review_queue': review,
            'orphan_predictions': {'baseline': before['orphan_predictions'], 'candidate': after['orphan_predictions']},
            'changed_samples': len(review), 'unchanged_samples': len(before['samples']) - len(review)}
