"""Explicit model changes, immutable provenance and administrator release decisions."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import base64
import hmac
import json
import re
import uuid

from fastapi import HTTPException

from .auth import audit, verify_sensitive_admin_session
from .security import decrypt_secret, encrypt_secret

STATE_KEY = '_model_configuration_v1'
MAX_CANDIDATES = 20
MAX_STATE_BYTES = 512 * 1024
MAX_REPORT_BYTES = 256 * 1024
PROVENANCE = ('model_id', 'model_revision', 'prompt_sha256', 'skill_version', 'hermes_version', 'tool_contract_version')
HASH = re.compile(r'^[0-9a-f]{64}$')
SAFE_VERSION = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def prompt_digest():
    from .hermes_client import SYSTEM_PROMPT
    return hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()


def _provenance(value):
    if not isinstance(value, dict) or set(value) != set(PROVENANCE):
        raise HTTPException(422, '模型来源版本字段不完整')
    if any(not isinstance(value[key], str) or not SAFE_VERSION.fullmatch(value[key]) for key in PROVENANCE if key != 'prompt_sha256'):
        raise HTTPException(422, '模型来源版本格式无效')
    if not isinstance(value['prompt_sha256'], str) or not HASH.fullmatch(value['prompt_sha256']):
        raise HTTPException(422, '提示摘要无效')
    return dict(value)


def legacy_state(settings, model):
    provider = getattr(settings, 'hermes_provider', 'custom:cline')
    provenance = {'model_id': model or 'hermes-agent',
        'model_revision': 'legacy_unattested', 'prompt_sha256': prompt_digest(),
        'skill_version': 'legacy_unattested', 'hermes_version': 'legacy_unattested', 'tool_contract_version': 'legacy_unattested'}
    active = {'model': model or None, 'provider': provider, 'provenance': provenance,
              'revision': 1, 'origin': 'migrated_legacy_baseline', 'artifact_verification': 'legacy_unattested'}
    active['version'] = digest(active)
    return {'schema': 1, 'revision': 1, 'active': active, 'candidates': [], 'last_consumption': None, 'consumed_versions': []}


def decode_state(settings, ciphertext):
    try:
        value = json.loads(decrypt_secret(settings.encryption_key, ciphertext))
        if (value.get('schema') != 1 or type(value.get('revision')) is not int or value['revision'] < 1
                or not isinstance(value.get('active'), dict) or not isinstance(value.get('candidates'), list)
                or len(value['candidates']) > MAX_CANDIDATES):
            raise ValueError
        active = value['active']
        if active.get('version') != digest({k: v for k, v in active.items() if k != 'version'}):
            raise ValueError
        _provenance(active['provenance'])
        consumed = value.get('consumed_versions', [])
        if (not isinstance(consumed, list) or len(consumed) > MAX_CANDIDATES+1
                or any(not isinstance(item, str) or not HASH.fullmatch(item) for item in consumed)):
            raise ValueError
        return value
    except Exception:
        raise HTTPException(503, '模型配置治理记录不可读，请核对密钥和版本') from None


async def lock(conn):
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext('model-configuration-v1'))")


async def read_state(conn, settings):
    rows = await conn.fetch('SELECT setting_key,encrypted_value FROM v2_admin_settings WHERE setting_key=ANY($1::text[])',
                            [STATE_KEY, 'hermes_analysis_model'])
    values = {row['setting_key']: row['encrypted_value'] for row in rows}
    if STATE_KEY in values:
        return decode_state(settings, values[STATE_KEY])
    model = ''
    if 'hermes_analysis_model' in values:
        try:
            model = decrypt_secret(settings.encryption_key, values['hermes_analysis_model'])
        except Exception:
            raise HTTPException(503, '原模型配置不可解密，不能覆盖迁移基线') from None
    return legacy_state(settings, model)


async def save_state(conn, settings, state, actor=None):
    raw = canonical(state)
    if len(raw) > MAX_STATE_BYTES:
        raise HTTPException(413, '模型治理记录达到上限，请先归档并保留审计')
    await conn.execute('INSERT INTO v2_admin_settings(setting_key,encrypted_value,updated_by,updated_at) VALUES($1,$2,$3,now()) '
        'ON CONFLICT(setting_key) DO UPDATE SET encrypted_value=EXCLUDED.encrypted_value,updated_by=EXCLUDED.updated_by,updated_at=now()',
        STATE_KEY, encrypt_secret(settings.encryption_key, raw.decode()), actor)


def public_state(state):
    active = state['active']
    applied = state.get('last_consumption')
    return {'revision': state['revision'], 'active': active, 'candidates': state['candidates'],
        'consumption': applied, 'application_state': 'consumed' if applied and applied['version'] == active['version'] else 'awaiting_consumer',
        'reason': '消费回执只证明工作器读取此快照，不等于提供方推理成功或技能部署已核验'}


def receipt_token(settings, snapshot):
    model = snapshot['model_configuration']
    claim = {'version': model['version'], 'configuration_version': snapshot['configuration_version'],
        'model': snapshot['model'], 'provider': model['provider'],
        'prompt_sha256': model['provenance']['prompt_sha256'], 'issued_at': int(datetime.now(timezone.utc).timestamp())}
    raw = canonical(claim)
    signature = hmac.new(settings.hermes_api_key.encode(), raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw+signature).decode().rstrip('=')


async def acknowledge(conn, settings, token, model, provider, prompt_sha256):
    try:
        raw = base64.b64decode(token+'='*(-len(token)%4), altchars=b'-_', validate=True)
        value, signature = raw[:-32], raw[-32:]
        if not hmac.compare_digest(signature, hmac.new(settings.hermes_api_key.encode(), value, hashlib.sha256).digest()):
            raise ValueError
        claim = json.loads(value)
        age = int(datetime.now(timezone.utc).timestamp()) - claim['issued_at']
        if not 0 <= age <= 600 or (claim['model'], claim['provider'], claim['prompt_sha256']) != (model, provider, prompt_sha256):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise HTTPException(409, '配置消费回执与已发快照不一致或已过期') from None
    await lock(conn)
    state = await read_state(conn, settings)
    accepted_versions = {state['active']['version']} | {item.get('activated_version') for item in state['candidates']} | {item['baseline_version'] for item in state['candidates']}
    if claim['version'] not in accepted_versions:
        raise HTTPException(409, '配置消费版本无有效激活记录')
    receipt = {**claim, 'consumed_at': datetime.now(timezone.utc).isoformat(), 'consumer': 'review_worker'}
    consumed = state.setdefault('consumed_versions', [])
    if receipt['version'] not in consumed:
        await audit(conn, None, 'system', 'model_configuration_consumed', 'model_configuration', receipt['version'], details=receipt)
        consumed.append(receipt['version'])
    if claim['version'] == state['active']['version']:
        state['last_consumption'] = receipt
    await save_state(conn, settings, state)
    return {'accepted': True, 'version': claim['version'], 'active_version': state['active']['version'],
        'current': claim['version'] == state['active']['version']}


async def stage(conn, settings, actor, model, *, provider=None, provenance=None, expected_revision=None, request_id=''):
    from .runtime_settings import validate_runtime_value
    await lock(conn)
    if actor is not None:
        await verify_sensitive_admin_session(conn, actor)
    state = await read_state(conn, settings)
    if expected_revision is not None and expected_revision != state['revision']:
        raise HTTPException(409, '模型配置版本已变化，请刷新后重试')
    try:
        model = validate_runtime_value('hermes_analysis_model', model)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    provider = provider or state['active']['provider']
    if provider != state['active']['provider']:
        raise HTTPException(422, '提供方必须与当前受控 Hermes 路由一致')
    provenance = _provenance(provenance or {**state['active']['provenance'],
        'model_id': model or getattr(settings, 'hermes_model', 'hermes-agent'), 'model_revision': 'candidate_unattested'})
    if provenance['model_id'] != (model or getattr(settings, 'hermes_model', 'hermes-agent')):
        raise HTTPException(422, '候选模型与来源声明不一致')
    spec = {'model': model or None, 'provider': provider, 'provenance': provenance,
            'baseline_version': state['active']['version']}
    fingerprint = digest(spec)
    if (spec['model'] == state['active']['model'] and provenance == state['active']['provenance']
            or any(item['fingerprint'] == fingerprint for item in state['candidates'])):
        raise HTTPException(409, '配置未变化或相同候选已经存在')
    if len(state['candidates']) >= MAX_CANDIDATES:
        raise HTTPException(413, '候选数量达到上限，不能丢弃旧审计')
    candidate = {'id': str(uuid.uuid4()), **spec, 'fingerprint': fingerprint, 'status': 'pending_evidence',
        'created_at': datetime.now(timezone.utc).isoformat(), 'created_by': str(actor['id']) if actor else None,
        'report': None, 'review': None}
    state['candidates'].append(candidate)
    state['revision'] += 1
    await save_state(conn, settings, state, actor['id'] if actor else None)
    await audit(conn, actor['id'] if actor else None, 'admin' if actor else 'system', 'model_candidate_created',
        'model_configuration', candidate['id'], request_id, {'candidate': candidate, 'active_unchanged': state['active']['version']})
    return candidate, state


def candidate_for(state, candidate_id):
    candidate = next((item for item in state['candidates'] if item['id'] == str(candidate_id)), None)
    if not candidate:
        raise HTTPException(404, '模型候选不存在')
    spec = {key: candidate[key] for key in ('model', 'provider', 'provenance', 'baseline_version')}
    if candidate['fingerprint'] != digest(spec):
        raise HTTPException(409, '模型候选来源已漂移')
    return candidate


def _safe_report(value, depth=0):
    if depth > 12:
        raise HTTPException(422, '比较报告层级超限')
    if isinstance(value, dict):
        if len(value) > 100:
            raise HTTPException(422, '比较报告字段超限')
        for key, item in value.items():
            if (not isinstance(key, str) or len(key) > 100 or key.lower() in {'headers','authorization','token','password','api_key','data_url'}):
                raise HTTPException(422, '比较报告包含不允许字段')
            _safe_report(item, depth+1)
    elif isinstance(value, list):
        if len(value) > 1000:
            raise HTTPException(422, '比较报告样本超限')
        for item in value:
            _safe_report(item, depth+1)
    elif isinstance(value, str) and (len(value) > 4096 or 'data:' in value.lower()):
        raise HTTPException(422, '比较报告字符串无效')


def compare_evidence(report, expected_sha, candidate, active):
    _safe_report(report)
    try:
        raw = canonical(report)
    except (ValueError, TypeError):
        raise HTTPException(422, '比较报告格式无效') from None
    if len(raw) > MAX_REPORT_BYTES:
        raise HTTPException(413, '比较报告超过字节上限')
    if not HASH.fullmatch(expected_sha) or hashlib.sha256(raw).hexdigest() != expected_sha:
        raise HTTPException(409, '比较报告摘要不匹配')
    if report.get('format') not in {'football-review-comparison-v1', 'football-review-comparison-real-v1'} or report.get('automatic_promotion') is not False:
        raise HTTPException(422, '比较报告版本或人工审批标记无效')
    if type(report.get('synthetic_only')) is not bool:
        raise HTTPException(422, '比较报告必须明确声明是否合成样本')
    for role, expected in [('baseline', active['provenance']), ('candidate', candidate['provenance'])]:
        declared = report.get(role+'_provenance', {})
        if not isinstance(declared, dict) or {key: declared.get(key) for key in PROVENANCE} != expected:
            raise HTTPException(409, '比较报告模型、提示或技能版本不匹配')
    if report.get('provenance_missing') != {'baseline': [], 'candidate': []}:
        raise HTTPException(422, '比较报告缺少来源版本')
    if not HASH.fullmatch(str(report.get('label_digest_sha256', ''))):
        raise HTTPException(422, '比较报告缺少冻结样本摘要')
    queue = report.get('review_queue')
    if not isinstance(queue, list) or report.get('changed_samples') != len(queue):
        raise HTTPException(422, '比较报告差异数量无效')
    ids = [item.get('sample_id') for item in queue if isinstance(item, dict)]
    if len(ids) != len(queue) or any(not isinstance(item, str) or not SAFE_VERSION.fullmatch(item) for item in ids) or len(set(ids)) != len(ids):
        raise HTTPException(422, '比较报告差异编号无效')
    synthetic = report.get('synthetic_only') is True
    if synthetic and report['format'] != 'football-review-comparison-v1':
        raise HTTPException(422, '合成报告版本与来源声明不一致')
    eligible = False
    source = report.get('source_evidence')
    if not synthetic:
        if report['format'] != 'football-review-comparison-real-v1' or not isinstance(source, dict):
            raise HTTPException(422, '真实比较报告必须提供人工标注来源声明')
        if (source.get('kind') != 'human_annotated' or source.get('labels_reviewed') is not True
                or not HASH.fullmatch(str(source.get('source_manifest_sha256', '')))
                or type(source.get('sample_count')) is not int or not 1 <= source['sample_count'] <= 1000
                or not isinstance(source.get('annotation_version'), str) or source['annotation_version'] != report.get('annotation_version')):
            raise HTTPException(422, '真实标注来源证据不完整')
        for role in ('baseline', 'candidate'):
            metrics = report.get(role+'_metrics')
            coverage = metrics.get('coverage') if isinstance(metrics, dict) else None
            if (not isinstance(coverage, dict) or type(coverage.get('samples')) is not int
                    or type(coverage.get('assessed')) is not int
                    or coverage.get('samples') != source['sample_count'] or coverage.get('assessed') != source['sample_count']
                    or coverage.get('missing', 0) or coverage.get('input_mismatch', 0) or coverage.get('unassessed', 0)):
                raise HTTPException(422, '真实比较报告未完整评估同一冻结样本')
        eligible = True
        if len(ids) > source['sample_count']:
            raise HTTPException(422, '比较差异数超过冻结样本数')
    snapshot = {'sha256': expected_sha, 'format': report['format'], 'synthetic_only': synthetic,
        'activation_eligible': eligible, 'dataset_id': str(report.get('dataset_id', ''))[:200],
        'annotation_version': str(report.get('annotation_version', ''))[:200], 'label_digest_sha256': report['label_digest_sha256'],
        'baseline_provenance': active['provenance'], 'candidate_provenance': candidate['provenance'],
        'metric_changes': report.get('metric_changes', {}), 'changed_sample_ids': ids,
        'differences': [{key: item[key] for key in ('sample_id','expected','baseline','candidate','metric_changes','reasons') if key in item} for item in queue],
        'changed_samples': len(ids), 'source_evidence': {key: source[key] for key in (
            'kind','labels_reviewed','source_manifest_sha256','sample_count','annotation_version')} if eligible else None,
        'interpretation': 'supplied_evidence_not_automatic_proof_of_label_truth'}
    snapshot['snapshot_sha256'] = digest(snapshot)
    return snapshot


def frozen_report(candidate, active):
    report = candidate['report']
    if report is None:
        return None
    if (report.get('snapshot_sha256') != digest({key: value for key, value in report.items() if key != 'snapshot_sha256'})
            or report['baseline_provenance'] != active['provenance']
            or report['candidate_provenance'] != candidate['provenance']):
        raise HTTPException(409, '冻结比较证据已漂移')
    return report


async def mutate(conn, settings, actor, candidate_id, expected_revision, operation, body, request_id=''):
    await lock(conn)
    await verify_sensitive_admin_session(conn, actor)
    state = await read_state(conn, settings)
    if state['revision'] != expected_revision:
        raise HTTPException(409, '模型配置版本已变化，请刷新后重试')
    candidate = candidate_for(state, candidate_id)
    if candidate['baseline_version'] != state['active']['version']:
        raise HTTPException(409, '候选基线已经变化，须重新冻结比较')
    if operation == 'evidence':
        if candidate['report'] is not None:
            raise HTTPException(409, '比较报告已冻结，不允许覆盖')
        candidate['report'] = compare_evidence(body['report'], body['report_sha256'], candidate, state['active'])
        candidate['status'] = 'awaiting_human_review' if candidate['report']['activation_eligible'] else 'synthetic_diagnostic_only'
    elif operation == 'review':
        report = frozen_report(candidate, state['active'])
        if not report or not report['activation_eligible'] or candidate['review']:
            raise HTTPException(409, '候选缺少真实比较证据或已经复核')
        if body['report_sha256'] != report['sha256'] or body['candidate_fingerprint'] != candidate['fingerprint']:
            raise HTTPException(409, '人工复核所引用证据已变化')
        decisions = body['decisions']
        if len(decisions) != len(report['changed_sample_ids']) or {item['sample_id'] for item in decisions} != set(report['changed_sample_ids']):
            raise HTTPException(422, '必须逐项复核全部差异，不能遗漏或重复')
        if any(item['decision'] != 'accept' for item in decisions):
            raise HTTPException(409, '存在未接受差异，候选不能激活')
        if len(body['reason'].strip()) < 3 or any(len(item['note'].strip()) < 3 for item in decisions):
            raise HTTPException(422, '人工复核说明不能为空白')
        candidate['review'] = {'actor_id': str(actor['id']), 'reviewed_at': datetime.now(timezone.utc).isoformat(),
            'report_sha256': report['sha256'], 'candidate_fingerprint': candidate['fingerprint'],
            'reason': body['reason'], 'decisions': decisions}
        candidate['status'] = 'reviewed'
    elif operation == 'activate':
        frozen_report(candidate, state['active'])
        if candidate['status'] != 'reviewed' or not candidate['report'] or not candidate['review']:
            raise HTTPException(409, '候选尚未完成证据和人工复核')
        if (not candidate['report']['activation_eligible'] or candidate['report']['synthetic_only']
                or candidate['report']['baseline_provenance'] != state['active']['provenance']
                or candidate['report']['candidate_provenance'] != candidate['provenance']
                or candidate['review']['report_sha256'] != candidate['report']['sha256']
                or candidate['review']['candidate_fingerprint'] != candidate['fingerprint']):
            raise HTTPException(409, '候选报告或人工复核来源已漂移')
        provenance = candidate['provenance']
        if (provenance['prompt_sha256'] != prompt_digest()
                or any(provenance[key] != state['active']['provenance'][key] for key in ('skill_version','hermes_version','tool_contract_version'))):
            raise HTTPException(409, 'artifact_deployment_unverified：提示、技能或框架部署证据尚未核验')
        state['revision'] += 1
        active = {'model': candidate['model'], 'provider': candidate['provider'], 'provenance': provenance,
            'revision': state['revision'], 'origin': 'reviewed_candidate', 'candidate_id': candidate['id'],
            'artifact_verification': state['active']['artifact_verification']}
        active['version'] = digest(active)
        state['active'] = active
        candidate['status'], candidate['activated_version'] = 'activated', active['version']
        await conn.execute("INSERT INTO v2_admin_settings(setting_key,encrypted_value,updated_by,updated_at) VALUES('hermes_analysis_model',$1,$2,now()) "
            "ON CONFLICT(setting_key) DO UPDATE SET encrypted_value=EXCLUDED.encrypted_value,updated_by=EXCLUDED.updated_by,updated_at=now()",
            encrypt_secret(settings.encryption_key, candidate['model'] or ''), actor['id'])
    else:
        raise ValueError('unsupported model configuration operation')
    if operation != 'activate':
        state['revision'] += 1
    await save_state(conn, settings, state, actor['id'])
    await audit(conn, actor['id'], 'admin', 'model_candidate_'+operation, 'model_configuration', candidate['id'], request_id,
        {'candidate_fingerprint': candidate['fingerprint'], 'candidate': candidate, 'active_version': state['active']['version'], 'revision': state['revision']})
    return public_state(state)
