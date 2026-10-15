"""Shared disk reserve and recovery gates for optional local work.

All workers serialize the small sample through one lock on the output volume.
API reads do not publish. No directory walk, network IO or history deletion is
performed here; collection and archive cleanup are never gated by this module.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any

GIB = 1024**3
SAMPLE_INTERVAL_S = 5
STATUS_MAX_AGE_S = 30


@dataclass(frozen=True)
class DiskPolicy:
    warning_bytes: int = 20 * GIB
    critical_bytes: int = 8 * GIB
    warning_ratio: float = .10
    critical_ratio: float = .05
    recovery_margin_bytes: int = 2 * GIB

    @classmethod
    def from_env(cls) -> DiskPolicy:
        policy = cls(
            int(os.environ.get('DISK_WARNING_FREE_BYTES', 20 * GIB)),
            int(os.environ.get('DISK_CRITICAL_FREE_BYTES', 8 * GIB)),
            float(os.environ.get('DISK_WARNING_FREE_RATIO', '.10')),
            float(os.environ.get('DISK_CRITICAL_FREE_RATIO', '.05')),
            int(os.environ.get('DISK_RECOVERY_MARGIN_BYTES', 2 * GIB)),
        )
        if (not 0 <= policy.critical_bytes <= policy.warning_bytes
                or not 0 <= policy.critical_ratio <= policy.warning_ratio <= 1
                or not all(math.isfinite(v) for v in (policy.critical_ratio, policy.warning_ratio))
                or policy.recovery_margin_bytes < 0):
            raise ValueError('磁盘水位配置无效')
        return policy


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _unknown(reason: str) -> dict[str, Any]:
    return {'at': datetime.now(timezone.utc).isoformat(), 'sampled_at': time.time(),
            'state': 'unknown', 'pause_optional_writes': True, 'reason': reason}


def _sample(root: Path, previous: dict[str, Any], policy: DiskPolicy) -> dict[str, Any]:
    disk = shutil.disk_usage(root)
    critical = max(policy.critical_bytes, math.ceil(disk.total * policy.critical_ratio))
    warning = max(policy.warning_bytes, math.ceil(disk.total * policy.warning_ratio))
    recovery = critical + policy.recovery_margin_bytes
    latched = previous.get('state') == 'critical'
    paused = disk.free <= critical or (latched and disk.free < recovery)
    state = 'critical' if paused else 'warning' if disk.free <= warning else 'normal'
    reason = ('磁盘空间临界，暂停新本地训练与可选备份，继续采集和安全归档' if paused else
              '磁盘空间偏低，归档清理继续运行' if state == 'warning' else '')
    return {'at': datetime.now(timezone.utc).isoformat(), 'sampled_at': time.time(),
            'state': state, 'pause_optional_writes': paused, 'reason': reason,
            'total': disk.total, 'used': disk.used, 'free': disk.free,
            'critical_free_bytes': critical, 'warning_free_bytes': warning,
            'recovery_free_bytes': recovery, 'policy': asdict(policy),
            'filesystem_device': root.stat().st_dev}


def disk_pressure(root: Path, *, publish: bool = True) -> dict[str, Any]:
    """Read/publish a shared sample; failures pause only optional new work."""
    root = root.resolve()
    state_dir = root / '_archive'
    path = state_dir / 'disk-pressure.json'
    try:
        policy = DiskPolicy.from_env()
        if not publish:
            previous = _read(path)
            age = time.time() - float(previous.get('sampled_at', 0))
            if not 0 <= age <= STATUS_MAX_AGE_S or previous.get('policy') != asdict(policy):
                return _unknown('磁盘保护状态缺失、过期或配置已变化，等待后台采样')
            return previous
        state_dir.mkdir(parents=True, exist_ok=True)
        with (state_dir / 'disk-pressure.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            previous = _read(path)
            age = time.time() - float(previous.get('sampled_at', 0))
            if (0 <= age < SAMPLE_INTERVAL_S and previous.get('policy') == asdict(policy)
                    and previous.get('state') in {'normal', 'warning', 'critical'}):
                return previous
            current = _sample(root, previous, policy)
            descriptor, temporary = tempfile.mkstemp(prefix='.disk-pressure-', dir=state_dir)
            try:
                with os.fdopen(descriptor, 'w') as stream:
                    json.dump(current, stream, ensure_ascii=False, allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return current
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        return _unknown('磁盘保护采样失败: ' + type(exc).__name__)
