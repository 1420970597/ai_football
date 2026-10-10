"""Independent model process: input extraction, inference, evaluation and training."""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any
import zlib

from core.learned_model import FEATURES, predict
from service.runtime_settings import RuntimeConfig, validated
from service.model_training import atomic_json

from store.training_data import snapshot_inputs, write_inputs


class ModelService:
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
        self._stats_process: subprocess.Popen[Any] | None = None
        self._cursor: dict[str, int] = {}
        self._settings_mtime = 0
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
        for filename, target in [('active.json', '_model'), ('status.json', '_status'), ('prospective.json', '_live_metrics'), ('storage.json', '_storage')]:
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
                self._shadow[key] = {**item, 'version': self._model['version'], 'probability': p,
                                     'predicted_at': datetime.fromtimestamp(time.time(), timezone.utc).isoformat()}

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
                if 'predicted_at' not in {r[1] for r in con.execute('PRAGMA table_info(predictions)')}:
                    con.execute('ALTER TABLE predictions ADD COLUMN predicted_at TEXT')
                con.executemany('INSERT OR IGNORE INTO predictions '
                    '(version,match_id,at,cutoff_ms,algorithm,market,line,outcome,baseline,x,probability,predicted_at) VALUES '
                    '(:version,:match_id,:at,:cutoff_ms,:algorithm,:market,:line,:outcome,:baseline,:x,:probability,:predicted_at)', pending.values())
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
        if self._stats_process and self._stats_process.poll() is None:
            self._stats_process.terminate()
            try:
                self._stats_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._stats_process.kill()
                self._stats_process.wait(timeout=3)
        self._thread = None
        self._flush_shadow()
        self._publish(False)

    def _reload_settings(self) -> None:
        if self.root is None:
            return
        path = self.root.parent / 'runtime-settings.json'
        if path.exists() and path.stat().st_mtime_ns != self._settings_mtime:
            data = json.loads(path.read_text())
            patch = {k: v for k, v in data['settings'].items() if k.startswith('model_training_')}
            self.configure(validated(self.cfg, patch))
            self._settings_mtime = path.stat().st_mtime_ns

    def _publish(self, running: bool = True) -> None:
        if self.root is None:
            return
        self._read()
        status = self.status()
        status.update(running=running, inference_running=running,
                      training_running=bool(self._process and self._process.poll() is None),
                      heartbeat_at=datetime.now(timezone.utc).isoformat(),
                      process_id=os.getpid(), service='model-worker')
        status['model'].pop('weights', None)
        status['model'].pop('cohort_ids', None)
        atomic_json(self.root.parent / 'models' / 'dashboard.json', status)

    def _consume_replay(self) -> None:
        if self.root is None:
            return
        cursor_path = self.root.parent / 'models' / 'replay-cursor.json'
        if not self._cursor and cursor_path.exists():
            self._cursor = json.loads(cursor_path.read_text())
        # Only schema-v2 immutable journals carry proven input cutoffs. Legacy
        # rows remain available to dataset() with explicitly missing trends.
        paths = sorted(self.root.glob('live-replay*.jsonl.gz'))
        for path in paths:
            offset = self._cursor.get(path.name, 0)
            if path.stat().st_size < offset:
                raise ValueError('永久决策日志被截断，不能跳过输入')
            if path.stat().st_size == offset:
                continue
            decoder = zlib.decompressobj(31)
            linebuf = b''
            with path.open('rb') as source:
                source.seek(offset)
                while not decoder.eof:
                    compressed = source.read(64 * 1024)
                    if not compressed:
                        return  # Writer is finishing a gzip member; retry it.
                    linebuf += decoder.decompress(compressed)
                    lines = linebuf.split(b'\n')
                    linebuf = lines.pop()
                    for line in lines:
                        if line:
                            self.observe(json.loads(line))
                        if len(self._inputs) >= 1000:
                            self._flush_shadow()
                            self._publish()
                if linebuf:
                    raise ValueError('决策归档成员缺少完整记录边界')
                end = source.tell() - len(decoder.unused_data)
            # Commit records before cursor. Replay after a crash is idempotent.
            self._flush_shadow()
            self._cursor[path.name] = end
            atomic_json(cursor_path, self._cursor)
            return  # Bound each loop to one member; heartbeat/training continue.

    def _loop(self) -> None:
        next_check = 0.0
        next_stats = 0.0
        while not self._stop.is_set():
            try:
                self._reload_settings()
                self._consume_replay()
                self._flush_shadow()
                self._publish()
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
                    if time.monotonic() >= next_stats and (self._stats_process is None or self._stats_process.poll() is not None):
                        self._stats_process = subprocess.Popen([sys.executable, '-m', 'service.model_service', '--stats', str(root)],
                                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        next_stats = time.monotonic() + 900
                    with self._lock:
                        self._read()
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


def main() -> None:
    root = Path(os.environ.get('MODEL_LEDGER_ROOT', '/app/output/ledger'))
    if len(sys.argv) == 3 and sys.argv[1] == '--stats':
        from service.data_model import storage_stats
        from service.model_training import constrain
        constrain(1, 256)
        atomic_json(Path(sys.argv[2]).parent / 'models' / 'storage.json', storage_stats(Path(sys.argv[2]).parent))
        return
    (root.parent / 'models').mkdir(parents=True, exist_ok=True)
    with (root.parent / 'models' / 'service.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        svc = ModelService()
        svc.bind(str(root))
        allowed = sorted(os.sched_getaffinity(0))
        os.sched_setaffinity(0, allowed[:2])
        os.nice(10)
        event = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: event.set())
        signal.signal(signal.SIGINT, lambda *_: event.set())
        svc.start()
        try:
            while not event.wait(1):
                if svc._thread is None or not svc._thread.is_alive():
                    raise RuntimeError('模型后台意外退出')
        finally:
            svc.stop()


if __name__ == '__main__':
    main()
