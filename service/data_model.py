"""Cached dashboard and supervised-model lifecycle. GET never scans or trains."""
from __future__ import annotations

from contextlib import closing

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
from typing import Any

from core.learned_model import FEATURES, predict
from service.runtime_settings import RuntimeConfig
from store.training_data import snapshot_inputs, write_inputs

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
    return {'at': datetime.now(timezone.utc).isoformat(), 'categories': categories,
            'bytes': sum(c['bytes'] for c in categories), 'files': sum(c['files'] for c in categories),
            'records': records, 'disk': {'total': disk.total, 'used': disk.used, 'free': disk.free}, 'errors': errors,
            'retention': '永久保存，不轮转、不删除；文件大小为压缩后的逻辑字节数'}


class DataModelService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[Any] | None = None
        self.root: Path | None = None
        self.cfg = RuntimeConfig()
        self._status: dict[str, Any] = {'state': 'not_started'}
        self._model: dict[str, Any] = {}
        self._storage: dict[str, Any] = {}
        self._shadow: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._inputs: list[dict[str, Any]] = []
        self._live_metrics: dict[str, Any] = {}
        self._read_times: dict[str, int] = {}
        self._process_started = 0.0
        self._retry_requested = False

    def bind(self, root: str | None) -> None:
        self.stop()
        with self._lock:
            self.root = Path(root) if root else None
            self._model = {}
            self._read_times.clear()
            self._live_metrics.clear()
            self._shadow.clear()
            self._inputs.clear()
            self._storage = {}
            self._status = {'state': 'not_started'}
            self._read()

    def configure(self, cfg: RuntimeConfig) -> None:
        with self._lock:
            previous = self.cfg
            self.cfg = cfg
            # New limits apply by terminating only our own worker and retrying with new limits.
            if self._process and self._process.poll() is None and (
                    not cfg.model_training_enabled or previous.model_training_cpu != cfg.model_training_cpu
                    or previous.model_training_memory_mb != cfg.model_training_memory_mb):
                self._retry_requested = True
                self._process.terminate()
        self._wake.set()

    def _read(self) -> None:
        if self.root is None:
            return
        read_error = ''
        for filename, target in [('active.json', '_model'), ('status.json', '_status'), ('prospective.json', '_live_metrics')]:
            path = self.root.parent / 'models' / filename
            if path.exists():
                try:
                    mtime = path.stat().st_mtime_ns
                    if self._read_times.get(filename) == mtime:
                        continue
                    data = json.loads(path.read_text(encoding='utf-8'))
                    if not isinstance(data, dict):
                        raise ValueError('无效模型元数据')
                    if target == '_model':
                        if data.get('features') != list(FEATURES) or not data.get('version'):
                            raise ValueError('模型特征版本不匹配')
                        predict(data['weights'], [0.0]*len(FEATURES))
                    setattr(self, target, data)
                    self._read_times[filename] = mtime
                except (OSError, ValueError, TypeError, KeyError):
                    read_error = '模型元数据读取失败'
        if read_error:
            self._status.update(state='failed', error=read_error)

    def status(self) -> dict[str, Any]:
        with self._lock:
            model = deepcopy({k: v for k, v in self._model.items() if k not in ('weights', 'cohort_ids')})
            status = deepcopy(self._status)
            prospective = deepcopy(self._live_metrics) if self._live_metrics.get('version') == self._model.get('version') else {}
            if not self.cfg.model_training_enabled:
                status['state'] = 'disabled'
            return {'model': model, 'training': status, 'storage': deepcopy(self._storage),
                    'settings': {'enabled': self.cfg.model_training_enabled,
                                 'cycle_matches': self.cfg.model_training_matches,
                                 'cpu_cores': self.cfg.model_training_cpu,
                                 'memory_mb': self.cfg.model_training_memory_mb},
                    'mode': 'shadow', 'running': bool(self._thread and self._thread.is_alive()),
                    'prospective': prospective,
                    'note': '独立观察预测，不参与真实投注。保留各算法、盘口、盘口线、方向和决策时点；按比赛分组、等权训练。走势特征冻结在决策输入截点，旧记录缺少走势时显式标记缺失。'}

    def observe(self, row: dict[str, Any]) -> None:
        inputs = snapshot_inputs(row)
        with self._lock:
            if self.root is None:
                return
            self._inputs.extend(inputs)
            if not self._model or row['computed_at'] <= self._model['updated_at']:
                return
            for item in inputs:
                key = tuple([self._model['version'], *[item[k] for k in ('match_id','at','algorithm','market','line','outcome')]])
                if key in self._shadow:
                    continue
                p = predict(self._model['weights'], json.loads(item['x']))
                self._shadow[key] = {**item, 'version': self._model['version'], 'probability': p}

    def _flush_shadow(self) -> None:
        with self._lock:
            if self.root is None:
                return
            pending, inputs = dict(self._shadow), list(self._inputs)
            path = self.root.parent / 'models' / 'shadow.sqlite3'
        write_inputs(path.parent / 'inputs.sqlite3', inputs)
        if pending:
            with closing(sqlite3.connect(path, timeout=2)) as con, con:
                con.execute('PRAGMA journal_mode=WAL')
                con.execute('PRAGMA synchronous=FULL')
                con.execute('CREATE TABLE IF NOT EXISTS predictions (version TEXT,match_id TEXT,at TEXT,cutoff_ms REAL,'
                    'algorithm TEXT,market TEXT,line TEXT,outcome TEXT,baseline REAL,x TEXT,probability REAL,'
                    'PRIMARY KEY(version,match_id,at,algorithm,market,line,outcome))')
                con.executemany('INSERT OR IGNORE INTO predictions VALUES '
                    '(:version,:match_id,:at,:cutoff_ms,:algorithm,:market,:line,:outcome,:baseline,:x,:probability)', pending.values())
        with self._lock:
            del self._inputs[:len(inputs)]
            for key in pending:
                self._shadow.pop(key, None)

    def start(self) -> None:
        if self.root is None or self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='data-model', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            process = self._process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        if self._thread and self._thread.is_alive():
            raise ValueError('数据与模型后台尚未停止')
        self._thread = None
        self._flush_shadow()

    def _loop(self) -> None:
        import time
        next_check = 0.0
        while not self._stop.is_set():
            try:
                self._flush_shadow()
                with self._lock:
                    cfg, root, process = self.cfg, self.root, self._process
                if root is None:
                    return
                if process is not None:
                    code = process.poll()
                    if code is None and time.monotonic() - self._process_started > 900:
                        process.kill()
                        code = process.wait(timeout=3)
                    with self._lock:
                        self._read()
                        if code is not None:
                            if code and not self._retry_requested:
                                self._status.update(state='failed', error=self._status.get('error') or '训练进程被终止（资源限制或超时）')
                            self._process = None
                            next_check = 0 if self._retry_requested else time.monotonic() + 60
                            self._retry_requested = False
                if time.monotonic() >= next_check:
                    root.parent.mkdir(parents=True, exist_ok=True)
                    storage = storage_stats(root.parent)
                    with self._lock:
                        self._storage = storage
                        cfg = self.cfg
                        if cfg.model_training_enabled and self._process is None and not self._stop.is_set():
                            self._process = subprocess.Popen([sys.executable, '-m', 'service.model_training',
                                str(root.resolve()), str(cfg.model_training_matches), str(cfg.model_training_cpu),
                                str(cfg.model_training_memory_mb)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            self._process_started = time.monotonic()
                            self._status = {**self._status, 'state': 'checking'}
                    next_check = time.monotonic() + 60
            except (OSError, ValueError, sqlite3.Error) as exc:
                with self._lock:
                    self._status.update(state='failed', error=str(exc))
                next_check = time.monotonic() + 60
            self._stop.wait(1)
            if self._wake.is_set():
                self._wake.clear()
                next_check = 0
        try:
            self._flush_shadow()
        except (OSError, sqlite3.Error) as exc:
            with self._lock:
                self._status.update(state='failed', error='模型输入保存失败: ' + str(exc))
