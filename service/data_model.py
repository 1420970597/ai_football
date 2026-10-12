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
from store.postgres import connect as pg_connect, postgres_enabled

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
    if postgres_enabled():
        try:
            with closing(pg_connect()) as pg_db:
                entries, matches, settled = pg_db.execute(
                    "SELECT count(*),count(DISTINCT match_id),"
                    "sum(CASE WHEN status IN ('won','lost','half_won','half_lost','push') THEN 1 ELSE 0 END) "
                    "FROM decisions").fetchone() or (0, 0, 0)
                records = {'decisions': int(entries or 0), 'matches': int(matches or 0),
                           'settled_decisions': int(settled or 0)}
        except Exception as exc:
            errors.append('PostgreSQL 台账不可读: ' + str(exc))
    elif db.exists():
        try:
            with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as local_db, local_db:
                entries, matches, settled = local_db.execute("SELECT count(*),count(DISTINCT match_id),"
                    "sum(status IN ('won','lost','half_won','half_lost','push')) FROM decisions").fetchone() or (0, 0, 0)
                records = {'decisions': entries, 'matches': matches, 'settled_decisions': settled or 0}
        except sqlite3.Error as exc:
            errors.append(str(exc))
    disk = shutil.disk_usage(root)
    archive: dict[str, Any] = {'backend': 'local'}
    catalog = root / '_archive' / 'catalog.sqlite3'
    try:
        if postgres_enabled():
            with closing(pg_connect()) as pg_db:
                file_row = pg_db.execute(
                    'SELECT count(*),coalesce(sum(size),0),coalesce(sum(cleaned),0) FROM files').fetchone() or (0, 0, 0)
                pack_row = pg_db.execute(
                    'SELECT count(*),coalesce(sum(bytes),0) FROM packs').fetchone() or (0, 0)
                file_count, remote_bytes, cleaned = file_row
                packs, object_bytes = pack_row
            archive = {'backend': 's3', 'catalog_backend': 'postgres', 'files': int(file_count or 0),
                       'bytes': int(remote_bytes or 0), 'packs': int(packs or 0),
                       'object_bytes': int(object_bytes or 0), 'cleaned_files': int(cleaned or 0)}
        elif catalog.exists():
            with closing(sqlite3.connect(catalog.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as local_db:
                file_count, remote_bytes, cleaned = local_db.execute('SELECT count(*),coalesce(sum(size),0),coalesce(sum(cleaned),0) FROM files').fetchone() or (0, 0, 0)
                packs, object_bytes = local_db.execute('SELECT count(*),coalesce(sum(bytes),0) FROM packs').fetchone() or (0, 0)
            archive = {'backend': 's3', 'files': file_count, 'bytes': remote_bytes,
                       'packs': packs, 'object_bytes': object_bytes, 'cleaned_files': cleaned}
    except Exception as exc:
        errors.append('归档索引不可读: ' + str(exc))
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
            out['mode'] = 'shadow' if self.cfg.model_shadow_enabled else 'active'
            out['decision_mode'] = self.cfg.decision_mode
            out['settings'] = {'enabled': self.cfg.model_training_enabled, 'shadow_enabled': self.cfg.model_shadow_enabled,
                               'decision_mode': self.cfg.decision_mode,
                               'model_blend_weight': self.cfg.model_blend_weight,
                               'cycle_matches': self.cfg.model_training_matches,
                               'cpu_cores': self.cfg.model_training_cpu,
                               'memory_mb': self.cfg.model_training_memory_mb}
            if self.root is not None:
                repair_path = self.root.parent / '_archive' / 'repairs' / 'task42' / 'status.json'
                try:
                    if repair_path.exists():
                        repair = json.loads(repair_path.read_text())
                        out['history_repair'] = repair
                        if repair.get('phase') == 'repairing' and not alive and self.cfg.model_training_enabled:
                            out['training']['state'] = 'restoring'
                            out['training']['error'] = ''
                            out['note'] = '历史数据正在恢复，完成后重新训练；实际模式在没有合格模型时使用经济算法。'
                except (OSError, ValueError):
                    out['history_repair'] = {'error':'历史修复状态暂不可读取'}
                disk = shutil.disk_usage(self.root.parent)
                out.setdefault('storage', {})['disk'] = {'total': disk.total, 'used': disk.used, 'free': disk.free}
                path = self.root.parent / '_archive' / 'status.json'
                try:
                    if path.exists():
                        archive_status = json.loads(path.read_text())
                        archive = out.setdefault('storage', {}).setdefault('archive', {})
                        archive.update(archive_status)
                except (OSError, ValueError):
                    out.setdefault('storage', {}).setdefault('archive', {})['error'] = 'S3 迁移状态不可读取'
                progress = path.with_name('progress.json')
                if progress.exists():
                    try:
                        out.setdefault('storage', {}).setdefault('archive', {})['migration'] = json.loads(progress.read_text())
                    except (OSError, ValueError):
                        pass
            storage = out.setdefault('storage', {})
            storage['summary_at'] = storage.get('at')
            try:
                storage_age = (datetime.now(timezone.utc)-datetime.fromisoformat(storage.get('at',''))).total_seconds()
                storage['summary_status'] = 'ready' if 0 <= storage_age < 1800 else 'stale'
            except (ValueError, TypeError):
                storage['summary_status'] = 'unavailable'
            if storage.get('errors'):
                storage['summary_status'] = 'error'
            storage['summary_reason'] = ('；'.join(storage.get('errors') or []) or
                ('等待独立模型服务更新存储统计' if storage['summary_status'] != 'ready' else ''))
            return out
