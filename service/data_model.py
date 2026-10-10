"""Read-only model dashboard; no model execution or lifecycle in the API."""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import threading
from typing import Any

from service.runtime_settings import RuntimeConfig

CATEGORIES = {'采集快照': 'snapshots', '走势': '_trends', '赛况与实际赛果': '_live', '决策与结算台账': 'ledger', '模型版本': 'models'}


def storage_stats(root: Path) -> dict[str, Any]:
    categories = []
    errors: list[str] = []
    for label, name in CATEGORIES.items():
        count = size = allocated = 0
        def error(exc: OSError) -> None:
            errors.append(str(exc))
        if not (root / name).exists():
            categories.append({'name': label, 'files': 0, 'bytes': 0, 'allocated_bytes': 0})
            continue
        for directory, _, files in os.walk(root / name, followlinks=False, onerror=error):
            for filename in files:
                path = Path(directory) / filename
                try:
                    if path.is_symlink():
                        continue
                    stat = path.stat()
                    count += 1
                    size += stat.st_size
                    allocated += stat.st_blocks * 512
                except OSError as exc:
                    errors.append(str(exc))
        categories.append({'name': label, 'files': count, 'bytes': size, 'allocated_bytes': allocated})
    # Legacy archives / latest caches / sqlite sidecars also count towards persistent storage.
    count = size = allocated = 0
    for path in root.iterdir():
        if path.is_file() and not path.is_symlink():
            stat = path.stat()
            count += 1
            size += stat.st_size
            allocated += stat.st_blocks * 512
    categories.append({'name': '旧归档与当前缓存', 'files': count, 'bytes': size, 'allocated_bytes': allocated})
    records: dict[str, Any] = {}
    db = root / 'ledger' / 'ledger.sqlite3'
    if db.exists():
        try:
            with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as con, con:
                entries, matches, settled = con.execute("SELECT count(*),count(DISTINCT match_id),"
                    "sum(status IN ('won','lost','half_won','half_lost','push')) FROM decisions").fetchone()
                records = {'decisions': entries, 'matches': matches, 'settled_decisions': settled or 0}
        except sqlite3.Error as exc:
            errors.append(str(exc))
    disk = shutil.disk_usage(root)
    archive: dict[str, Any] = {'backend': 'local'}
    catalog = root / '_archive' / 'catalog.sqlite3'
    if catalog.exists():
        with closing(sqlite3.connect(catalog.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as con:
            files, remote_bytes, cleaned = con.execute('SELECT count(*),coalesce(sum(size),0),coalesce(sum(cleaned),0) FROM files').fetchone()
            packs, object_bytes = con.execute('SELECT count(*),coalesce(sum(bytes),0) FROM packs').fetchone()
        archive = {'backend': 's3', 'files': files, 'bytes': remote_bytes,
                   'packs': packs, 'object_bytes': object_bytes, 'cleaned_files': cleaned}
    return {'at': datetime.now(timezone.utc).isoformat(), 'categories': categories, 'archive': archive,
            'bytes': sum(c['bytes'] for c in categories), 'files': sum(c['files'] for c in categories),
            'records': records, 'disk': {'total': disk.total, 'used': disk.used, 'free': disk.free}, 'errors': errors,
            'retention': '归档永久保存；S3 模式在上传回读校验后清理本地历史副本。运行状态与索引保留本地。'}


class DataModelService:
    """A view over an independent worker's small atomic status document."""

    def __init__(self) -> None:
        self.root: Path | None = None
        self.cfg = RuntimeConfig()
        self._lock = threading.RLock()
        self._cached: dict[str, Any] = {}
        self._stamp = 0

    def bind(self, root: str | None) -> None:
        with self._lock:
            self.root = Path(root) if root else None
            self._cached = {}
            self._stamp = 0

    def configure(self, cfg: RuntimeConfig) -> None:
        with self._lock:
            self.cfg = cfg

    def status(self) -> dict[str, Any]:
        with self._lock:
            error = ''
            if self.root is not None:
                path = self.root.parent / 'models' / 'dashboard.json'
                try:
                    if path.exists():
                        data = json.loads(path.read_text())
                        if not isinstance(data, dict) or data.get('service') != 'model-worker':
                            raise ValueError('无效模型服务状态')
                        self._cached = data
                except (OSError, ValueError, TypeError):
                    error = '模型服务状态不可读取'
            out: dict[str, Any] = deepcopy(self._cached) if self._cached else {
                'model': {}, 'training': {'state': 'not_started'}, 'storage': {},
                'prospective': {}, 'mode': 'shadow', 'running': False,
                'training_running': False, 'inference_running': False,
                'note': '独立模型服务负责训练与观察预测；系统服务仅采集、归档与展示。'}
            try:
                at = datetime.fromisoformat(out.get('heartbeat_at', ''))
                alive = 0 <= (datetime.now(timezone.utc) - at).total_seconds() <= 90 and out.get('running')
            except (TypeError, ValueError):
                alive = False
            out['running'] = bool(alive)
            if not alive:
                out['training_running'] = out['inference_running'] = False
                out['training'] = {**out['training'], 'state': 'not_started',
                                   'error': error or '独立模型服务未运行或心跳已过期'}
            elif error:
                out['training']['error'] = error
            if not self.cfg.model_training_enabled:
                out['training']['state'] = 'disabled'
            out['settings'] = {'enabled': self.cfg.model_training_enabled,
                               'cycle_matches': self.cfg.model_training_matches,
                               'cpu_cores': self.cfg.model_training_cpu,
                               'memory_mb': self.cfg.model_training_memory_mb}
            if self.root is not None:
                disk = shutil.disk_usage(self.root.parent)
                out.setdefault('storage', {})['disk'] = {'total': disk.total, 'used': disk.used, 'free': disk.free}
                path = self.root.parent / '_archive' / 'status.json'
                try:
                    if path.exists():
                        out.setdefault('storage', {})['archive'] = json.loads(path.read_text())
                except (OSError, ValueError):
                    out.setdefault('storage', {})['archive'] = {'error': 'S3 迁移状态不可读取'}
                progress = path.with_name('progress.json')
                if progress.exists():
                    try:
                        out.setdefault('storage', {}).setdefault('archive', {})['migration'] = json.loads(progress.read_text())
                    except (OSError, ValueError):
                        pass
            return out
