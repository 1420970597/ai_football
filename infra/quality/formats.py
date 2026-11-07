"""Small explicit synthetic label/prediction formats; no implicit zero filling."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
import re

FIELDS = ('fixture_id', 'market_id', 'market_kind', 'period', 'line', 'selection', 'phase')
CHOICES = {'market_kind': {'winner', 'handicap', 'total'}, 'period': {'full_time', 'first_half', 'second_half'},
           'selection': {'home', 'away', 'draw', 'over', 'under'}, 'phase': {'scheduled', 'live'}}
PROVENANCE_FIELDS = ('model_id', 'model_revision', 'prompt_sha256', 'skill_version', 'hermes_version',
                     'tool_contract_version', 'produced_at')
MAX_SAMPLES = 1000
MAX_ITEMS = 32
NO_LINE = 'not_applicable'  # Internal semantic value; explicit winner NULL differs from a missing key.
HASH = re.compile(r'^[a-f0-9]{64}$')
IDENTIFIER = re.compile(r'^[A-Za-z0-9_.:-]{1,160}$')


class QualityError(ValueError):
    pass


def identifier(value, category):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise QualityError(category)
    return value


def aware(value):
    try:
        return isinstance(value, str) and datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is not None
    except ValueError:
        return False


def normalize_item(item, *, expected=False):
    if not isinstance(item, dict):
        raise QualityError('recommendations_must_be_objects')
    allowed = set(FIELDS) | {'intent_id', 'signal_id', 'duplicate'}
    if set(item) - allowed:
        raise QualityError('unknown_recommendation_fields')
    result, missing, invalid = {}, [], []
    for field in FIELDS:
        value = item.get(field)
        if field == 'line' and item.get('market_kind') == 'winner' and 'line' in item and value is None:
            result[field] = NO_LINE
            continue
        if value is None or value == '':
            result[field] = None
            missing.append(field)
            continue
        if field == 'line':
            try:
                if isinstance(value, bool):
                    raise InvalidOperation
                number = Decimal(str(value))
                if not number.is_finite() or abs(number) > 1000000:
                    raise InvalidOperation
                result[field] = format((number if number else Decimal(0)).normalize(), 'f')
                if item.get('market_kind') == 'winner':
                    if number != 0:
                        raise InvalidOperation
                    result[field] = NO_LINE
            except (InvalidOperation, ValueError):
                result[field] = None
                invalid.append(field)
        elif field in CHOICES:
            if not isinstance(value, str) or value not in CHOICES[field]:
                result[field] = None
                invalid.append(field)
            else:
                result[field] = value
        elif isinstance(value, (str, int)) and not isinstance(value, bool) and IDENTIFIER.fullmatch(str(value)):
            result[field] = str(value)
        else:
            result[field] = None
            invalid.append(field)
    if expected and (missing or invalid):
        raise QualityError('gold_recommendation_has_missing_or_invalid_binding')
    return result, missing, invalid


def identity(item):
    return tuple(item[field] for field in FIELDS)


def validate_labels(data):
    if not isinstance(data, dict) or data.get('format') != 'football-review-labels-v1' or data.get('synthetic') is not True:
        raise QualityError('explicit_synthetic_labels_required')
    identifier(data.get('dataset_id'), 'invalid_dataset_id')
    identifier(data.get('annotation_version'), 'missing_annotation_version')
    if data.get('label_schema_version') != 1 or not aware(data.get('created_at')):
        raise QualityError('invalid_label_schema_or_creation_time')
    rows = data.get('samples')
    if not isinstance(rows, list) or not rows or len(rows) > MAX_SAMPLES:
        raise QualityError('label_sample_budget_invalid')
    seen, normalized, total = set(), [], 0
    for row in rows:
        if not isinstance(row, dict) or row.get('synthetic') is not True:
            raise QualityError('every_sample_must_be_explicitly_synthetic')
        sample_id = identifier(row.get('sample_id'), 'invalid_sample_id')
        if sample_id in seen:
            raise QualityError('duplicate_label_sample_id')
        seen.add(sample_id)
        source = row.get('source')
        if (not isinstance(source, dict) or source.get('level') != 'synthetic'
                or not isinstance(source.get('reference'), str) or not source['reference'].startswith('synthetic:')
                or not HASH.fullmatch(str(source.get('content_sha256', '')))
                or not isinstance(source.get('message_revision'), int) or isinstance(source['message_revision'], bool)
                or source['message_revision'] < 1):
            raise QualityError('invalid_synthetic_source_evidence')
        media = source.get('media_sha256', [])
        if not isinstance(media, list) or any(not isinstance(value, str) or not HASH.fullmatch(value) for value in media):
            raise QualityError('invalid_media_evidence_hashes')
        if row.get('modality') not in {'text', 'image', 'mixed'}:
            raise QualityError('invalid_sample_modality')
        if row['modality'] in {'image', 'mixed'} and not media:
            raise QualityError('visual_gold_requires_media_hash_evidence')
        expected = row.get('expected')
        if not isinstance(expected, dict) or expected.get('decision') not in {'accept', 'reject'}:
            raise QualityError('explicit_gold_decision_required')
        picks = expected.get('recommendations')
        if not isinstance(picks, list) or len(picks) > MAX_ITEMS:
            raise QualityError('gold_recommendation_budget_invalid')
        if bool(picks) != (expected['decision'] == 'accept'):
            raise QualityError('gold_decision_and_recommendations_conflict')
        if not picks and (not isinstance(expected.get('rejection_reason'), str) or not expected['rejection_reason'].strip()):
            raise QualityError('gold_rejection_reason_required')
        normalized_picks, identities, intent_ids = [], set(), set()
        for pick in picks:
            parsed, _, _ = normalize_item(pick, expected=True)
            intent = identifier(pick.get('intent_id'), 'gold_intent_id_required')
            if identity(parsed) in identities or intent in intent_ids:
                raise QualityError('duplicate_gold_recommendation_identity')
            identities.add(identity(parsed))
            intent_ids.add(intent)
            normalized_picks.append(parsed | {'intent_id': intent})
        total += len(picks)
        normalized.append({**row, 'expected': {**expected, 'recommendations': normalized_picks}})
    if total > 10000:
        raise QualityError('total_gold_recommendation_budget_exceeded')
    return {**data, 'samples': normalized}


def validate_run(data, dataset_id):
    if (not isinstance(data, dict) or data.get('format') != 'football-review-predictions-v1'
            or data.get('synthetic') is not True or data.get('dataset_id') != dataset_id):
        raise QualityError('synthetic_run_must_reference_same_dataset')
    identifier(data.get('run_id'), 'invalid_run_id')
    provenance = data.get('provenance', {})
    if not isinstance(provenance, dict):
        raise QualityError('run_provenance_must_be_object')
    missing = [field for field in PROVENANCE_FIELDS if not isinstance(provenance.get(field), str) or not provenance[field].strip()]
    if 'prompt_sha256' not in missing and not HASH.fullmatch(provenance['prompt_sha256']):
        missing.append('prompt_sha256_invalid')
    if 'produced_at' not in missing and not aware(provenance['produced_at']):
        missing.append('produced_at_invalid')
    rows = data.get('samples')
    if not isinstance(rows, list) or len(rows) > MAX_SAMPLES:
        raise QualityError('prediction_sample_budget_invalid')
    seen, normalized = set(), []
    for row in rows:
        if not isinstance(row, dict):
            raise QualityError('prediction_samples_must_be_objects')
        sample_id = identifier(row.get('sample_id'), 'invalid_prediction_sample_id')
        if sample_id in seen:
            raise QualityError('duplicate_prediction_sample_id')
        seen.add(sample_id)
        accepted = row.get('accepted')
        if accepted is not None and (not isinstance(accepted, list) or len(accepted) > 64):
            raise QualityError('accepted_recommendation_budget_invalid')
        parsed, groups, issues, signal_identities = [], {}, [], {}
        for index, item in enumerate(accepted or []):
            pick, absent, invalid = normalize_item(item)
            key = identity(pick)
            signal_id = item.get('signal_id')
            if signal_id is not None:
                identifier(signal_id, 'invalid_signal_id')
                if signal_id in signal_identities and signal_identities[signal_id] != key:
                    raise QualityError('same_signal_id_has_conflicting_bindings')
                signal_identities[signal_id] = key
            issues.extend(f'accepted[{index}].{field}:missing' for field in absent)
            issues.extend(f'accepted[{index}].{field}:invalid' for field in invalid)
            if key not in groups:
                groups[key] = pick
                parsed.append(pick)
        if len(parsed) > MAX_ITEMS:
            raise QualityError('unique_accepted_budget_exceeded')
        status = row.get('status')
        if status is not None and (not isinstance(status, str) or status not in {'completed', 'error'}):
            raise QualityError('unsupported_prediction_status')
        normalized.append({**row, 'normalized_accepted': parsed, 'accepted_supplied': isinstance(accepted, list),
                           'duplicate_outputs': len(accepted or []) - len(parsed),
                           'duplicate_acknowledgements': sum(item.get('duplicate') is True for item in accepted or []),
                           'missing_fields': issues})
    return {**data, 'samples': normalized, 'provenance_missing': sorted(missing)}
