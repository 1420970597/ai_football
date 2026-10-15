"""Resource-gated remote training dispatch with verified model return."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import re
import subprocess
from typing import Any

from core.learned_model import FEATURES, compatible_model, predict

REMOTE_TRAINING_THRESHOLD_BYTES = 100 * 1024**3
REMOTE_TRAINING_TIMEOUT_S = int(os.environ.get('MODEL_REMOTE_TRAINING_TIMEOUT_S', '86400'))


def estimated_temp_bytes(root: Path) -> int | None:
    """Missing is unknown, never zero. Malformed estimates block execution."""
    configured = os.environ.get('MODEL_TRAINING_ESTIMATED_TEMP_BYTES', '').strip()
    if configured:
        value = int(configured)
    else:
        marker = root.parent / 'models' / 'training-estimate.json'
        try:
            data = json.loads(marker.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None
        if not isinstance(data, dict) or type(data.get('peak_temp_bytes')) is not int:
            raise ValueError('训练空间估算格式无效')
        value = int(data['peak_temp_bytes'])
    if value <= 0:
        raise ValueError('训练空间估算必须为正整数')
    return value


def local_pipeline_estimate(memory_mb: int, *, sqlite_bytes: int = 0) -> int:
    """Conservative estimate for indexed PG + memory-only fitting, not a measurement.

    This path writes small JSON artifacts/logs and never restores S3 history.
    A future pipeline that stages datasets must supply its own explicit estimate.
    """
    if memory_mb <= 0 or sqlite_bytes < 0:
        raise ValueError('训练内存限制必须为正整数')
    return 4 * memory_mb * 1024**2 + 64 * 1024**2 + 2 * sqlite_bytes


def remote_training_required(estimate: int) -> bool:
    return int(estimate) > REMOTE_TRAINING_THRESHOLD_BYTES


def _model_from_response(response: dict[str, Any], root: Path) -> dict[str, Any]:
    model = response.get('model') or response.get('artifact_json')
    if model is None and response.get('artifact_path'):
        artifact = Path(str(response['artifact_path'])).resolve()
        allowed = (root.parent / 'models' / 'remote-artifacts').resolve()
        if not artifact.is_relative_to(allowed):
            raise ValueError('远程制品路径必须位于 models/remote-artifacts 内')
        model = json.loads(artifact.read_text(encoding='utf-8'))
    if not isinstance(model, dict) or not compatible_model(model):
        raise ValueError('远程训练回传的模型制品未通过兼容性核验')
    predict(model.get('weights', []), [0.] * len(FEATURES))
    expected = response.get('sha256')
    if not isinstance(expected, str) or re.fullmatch(r'[0-9a-fA-F]{64}', expected) is None:
        raise ValueError('远程训练模型制品必须包含 SHA256')
    if expected:
        encoded = json.dumps(model, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        if hashlib.sha256(encoded).hexdigest() != str(expected).lower():
            raise ValueError('远程训练模型制品 SHA256 不匹配')
    return model


def dispatch(root: Path, *, cycle: int, cpu: int, memory_mb: int,
             estimate_bytes: int, cohort: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the configured S3-side launcher and validate its JSON response.

    The launcher owns S3 staging and remote resource limits. It receives only
    a manifest on stdin and must return one JSON object containing ``model`` or
    ``artifact_path`` plus its required canonical JSON SHA256.
    """
    raw_command = os.environ.get('MODEL_REMOTE_TRAINING_COMMAND', '').strip()
    if not raw_command:
        raise RuntimeError(
            '训练峰值临时空间超过100GiB；未配置 MODEL_REMOTE_TRAINING_COMMAND，已阻止本地训练')
    manifest = {
        'protocol': 1,
        'root': str(root),
        'cycle_matches': int(cycle),
        'cpu_cores': int(cpu),
        'memory_mb': int(memory_mb),
        'estimated_temp_bytes': int(estimate_bytes),
        'cohort': cohort or {},
        'delivery': 'json-on-stdout; launcher must stage inputs on S3 and return verified model',
    }
    result = subprocess.run(
        shlex.split(raw_command), input=json.dumps(manifest, ensure_ascii=False),
        text=True, capture_output=True, timeout=REMOTE_TRAINING_TIMEOUT_S, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError('远程训练命令失败，退出码 %s' % result.returncode)
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError('远程训练未回传模型制品')
    try:
        response = json.loads(lines[-1])
    except (ValueError, TypeError) as exc:
        raise RuntimeError('远程训练回传不是 JSON 模型制品') from exc
    if not isinstance(response, dict):
        raise RuntimeError('远程训练回传格式无效')
    return _model_from_response(response, root)
