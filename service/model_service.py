"""Independent model process: input extraction, inference, evaluation and training."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Any
import zlib

from core.learned_model import FEATURES, compatible_model, predict
from service.runtime_settings import RuntimeConfig, validated
from service.model_training import atomic_json

from store.training_data import (snapshot_inputs, write_inputs, write_predictions,
                                 prospective_metrics, prospective_prediction_rows)

REPLAY_MEMBERS_PER_CYCLE = 128
TRAINING_WALL_LIMIT_S = int(os.environ.get('MODEL_TRAINING_WALL_LIMIT_S', '3600'))


def hashlib_version(version: str) -> str:
    return hashlib.sha256(str(version).encode()).hexdigest()[:24]


class ModelService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[Any] | None = None
        self._process_log_path: Path | None = None
        self.root: Path | None = None
        self.cfg = RuntimeConfig()
        self._status: dict[str, Any] = {'state': 'not_started'}
        self._model: dict[str, Any] = {}
        self._shadow_model: dict[str, Any] = {}
        self._shadow_info: dict[str, Any] = {}
        self._candidate_cache: dict[str, tuple[tuple[int, int, int], dict[str, Any]]] = {}
        self._evaluation_state: dict[str, Any] = {}
        self._next_evaluation = 0.0
        self._storage: dict[str, Any] = {}
        self._shadow: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._inputs: list[dict[str, Any]] = []
        self._live_metrics: dict[str, Any] = {}
        self._read_times: dict[str, tuple[int, int, int]] = {}
        self._process_started = 0.0
        self._stats_process: subprocess.Popen[Any] | None = None
        self._cursor: dict[str, int] = {}
        self._settings_mtime = 0
        self._retry_requested = False
        self._replay_stats: dict[str, Any] = {
            'last_file': '', 'last_at': '', 'last_batch_records': 0,
            'last_batch_inputs': 0, 'total_records': 0, 'total_inputs': 0,
            'backlog_files': 0,
        }

    def bind(self, root: str | None) -> None:
        self.stop()
        with self._lock:
            self.root = Path(root) if root else None
            self._model = {}
            self._shadow_model = {}
            self._shadow_info = {}
            self._candidate_cache = {}
            self._evaluation_state = {}
            self._read_times.clear()
            self._live_metrics.clear()
            self._shadow.clear()
            self._inputs.clear()
            self._storage = {}
            self._status = {'state': 'not_started'}
            self._replay_stats = {
                'last_file': '', 'last_at': '', 'last_batch_records': 0,
                'last_batch_inputs': 0, 'total_records': 0, 'total_inputs': 0,
                'backlog_files': 0,
            }
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
            if target == '_model' and not path.exists():
                self._model = {}
                self._read_times.pop(filename, None)
            if path.exists():
                try:
                    stat = path.stat()
                    signature = (stat.st_mtime_ns, stat.st_ino, stat.st_size)
                    if self._read_times.get(filename) == signature:
                        continue
                    data = json.loads(path.read_text(encoding='utf-8'))
                    if not isinstance(data, dict):
                        raise ValueError('无效模型元数据')
                    if target == '_model':
                        if not compatible_model(data):
                            self._model = {}
                            raise ValueError('模型训练数据版本未通过核验')
                        predict(data['weights'], [0.0]*len(FEATURES))
                    setattr(self, target, data)
                    self._read_times[filename] = signature
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    if target == '_model':
                        self._model = {}
                    read_error = '模型元数据读取失败: ' + str(exc)
        if read_error:
            self._status.update(state='failed', error=read_error)
        # Observation may use an unapproved candidate, but this never changes
        # active.json or the live executor's approval requirement.
        model_root = self.root.parent / 'models'
        candidate_path = model_root / ('candidate-' + str(self._status.get('candidate_version') or '') + '.json')
        paths = ([candidate_path] if candidate_path.exists() else [])
        paths += sorted(model_root.glob('candidate-*.json'), reverse=True)
        paths += [model_root / 'active.json']
        chosen = {}
        reason = '尚无兼容的影子候选或已晋级模型'
        for path in dict.fromkeys(paths):
            if not path.exists():
                continue
            try:
                stat = path.stat()
                signature = (stat.st_mtime_ns,stat.st_ino,stat.st_size)
                cached = self._candidate_cache.get(str(path))
                if cached and cached[0] == signature:
                    data = cached[1]
                else:
                    data = json.loads(path.read_text())
                    if len(self._candidate_cache) >= 4:
                        self._candidate_cache.pop(next(iter(self._candidate_cache)))
                    self._candidate_cache[str(path)] = (signature,data)
                if not isinstance(data, dict) or not compatible_model(data):
                    raise ValueError('模型训练数据版本未通过核验')
                predict(data['weights'], [0.0]*len(FEATURES))
                at = datetime.fromisoformat(data['updated_at'])
                if at.tzinfo is None:
                    raise ValueError('模型更新时间缺少时区')
                if at > datetime.now(timezone.utc):
                    raise ValueError('模型尚未可用')
                chosen = {**data, 'artifact_kind': 'candidate' if path.name.startswith('candidate-') else 'active'}
                break
            except (OSError, ValueError, TypeError, KeyError) as exc:
                reason = str(exc)
        previous = self._shadow_model.get('version')
        if chosen.get('version') != previous:
            self._live_metrics = {}
            self._evaluation_state = {}
            self._next_evaluation = 0
            self._shadow_info.update(last_prediction_at=None,last_decision_at=None)
        self._shadow_model = chosen
        self._shadow_info = {**self._shadow_info, 'available': bool(chosen),
            'version': chosen.get('version'), 'artifact_kind': chosen.get('artifact_kind'),
            'approved': bool((chosen.get('promotion') or {}).get('approved')),
            'updated_at': chosen.get('updated_at'), 'available_at': chosen.get('updated_at'),
            'parameter_count': chosen.get('parameter_count'), 'architecture': chosen.get('architecture'),
            'reason': '' if chosen else reason}

    def status(self) -> dict[str, Any]:
        with self._lock:
            model = deepcopy({k: v for k, v in self._model.items() if k not in ('weights', 'cohort_ids')})
            status = deepcopy(self._status)
            prospective = deepcopy(self._live_metrics) if self._live_metrics.get('version') == self._shadow_model.get('version') else {}
            shadow = deepcopy(self._shadow_info)
            shadow['enabled'] = self.cfg.model_shadow_enabled
            if not self.cfg.model_shadow_enabled:
                shadow['reason'] = '影子预测记录已关闭；展示已有版本窗口，未生成新预测'
            if self._shadow_model:
                shadow['model'] = {k: deepcopy(v) for k,v in self._shadow_model.items()
                                   if k not in ('weights', 'cohort_ids')}
            if not self.cfg.model_training_enabled:
                status['state'] = 'disabled'
            return {'model': model, 'training': status, 'storage': deepcopy(self._storage),
                    'settings': {'enabled': self.cfg.model_training_enabled, 'shadow_enabled': self.cfg.model_shadow_enabled,
                                 'decision_mode': self.cfg.decision_mode,
                                 'model_blend_weight': self.cfg.model_blend_weight,
                                 'cycle_matches': self.cfg.model_training_matches,
                                 'cpu_cores': self.cfg.model_training_cpu,
                                 'memory_mb': self.cfg.model_training_memory_mb},
                    'mode': 'shadow' if self.cfg.model_shadow_enabled else 'active', 'running': bool(self._thread and self._thread.is_alive()),
                    'decision_mode': self.cfg.decision_mode,
                    'decision_model_available': bool(self._model and
                        isinstance(self._model.get('promotion'), dict) and
                        self._model['promotion'].get('approved') is True),
                    'prospective': prospective, 'shadow': shadow,
                    'note': '独立模型服务负责训练与观察预测；实时决策模式独立选择纯算法、纯决策模型或混合。纯模型是当前盘口offset与算法特征训练模型的输出，不进行额外算法加权。模型或混合模式缺少已晋级模型时不可用。保留各算法、盘口、盘口线、方向和决策时点；按比赛分组、等权训练。走势特征冻结在决策输入截点。'}

    def observe(self, row: dict[str, Any]) -> None:
        state = row.get('input_state') or {}
        if not row.get('received_at_ms') and state.get('received_at_ms'):
            row = {**row, 'received_at_ms': state['received_at_ms']}
        inputs = snapshot_inputs(row)
        with self._lock:
            if self.root is None:
                return
            self._inputs.extend(inputs)
            model = self._shadow_model
            if not inputs or not self.cfg.model_shadow_enabled or not model:
                return
            available = datetime.fromisoformat(model['updated_at'])
            decision_at = datetime.fromisoformat(row['computed_at'])
            if (decision_at.tzinfo is None or available.timestamp()*1000 > float(row['input_cutoff_ms'])
                    or decision_at < available):
                self._shadow_info['skipped_before_available'] = self._shadow_info.get('skipped_before_available',0)+len(inputs)
                return
            from core.oof_peer import calibrate_peer_vector
            from core.probability_calibration import calibrate_probability
            from core.model_validation import coherent_probabilities
            probabilities = [calibrate_probability(predict(model['weights'],
                calibrate_peer_vector(json.loads(item['x']),model.get('peer_calibration'))),
                model.get('calibration')) for item in inputs]
            probabilities = coherent_probabilities(inputs, probabilities)
            predicted_at = datetime.fromtimestamp(time.time(), timezone.utc).isoformat()
            for item, p in zip(inputs, probabilities):
                key = tuple([model['version'], *[item[k] for k in ('match_id','at','algorithm','market','line','outcome')]])
                if key in self._shadow:
                    continue
                self._shadow[key] = {**item, 'version': model['version'], 'probability': p,
                                     'predicted_at': predicted_at}
            if inputs:
                self._shadow_info.update(last_prediction_at=predicted_at,last_decision_at=row['computed_at'])

    def _flush_shadow(self) -> None:
        with self._lock:
            if self.root is None:
                return
            pending, inputs = dict(self._shadow), list(self._inputs)
            path = self.root.parent / 'models' / 'shadow.sqlite3'
        # write_inputs/write_predictions select the configured projection DSN.
        # With no DSN they preserve the existing local SQLite files; with a PG
        # DSN no model data is written to local SQLite at all.
        write_inputs(path.parent / 'inputs.sqlite3', inputs)
        prospective = prospective_prediction_rows(self.root / 'ledger.sqlite3', list(pending.values()))
        write_predictions(path, prospective)
        with self._lock:
            del self._inputs[:len(inputs)]
            for key in pending:
                self._shadow.pop(key, None)
            self._shadow_info['predictions_written'] = self._shadow_info.get('predictions_written',0)+len(prospective)
            self._shadow_info['skipped_settled_replay'] = self._shadow_info.get('skipped_settled_replay',0)+len(pending)-len(prospective)
            self._shadow_info['predictions_written_scope'] = '本进程提交写入条数，包含幂等重放；窗口数量以prospective.counts为准'

    def _evaluate_shadow(self) -> None:
        if self.root is None or time.monotonic() < self._next_evaluation:
            return
        model = self._shadow_model
        if not model:
            self._live_metrics = {'status': 'unavailable', 'reason': self._shadow_info.get('reason'),
                                  'version': None}
            return
        state_path = self.root.parent / 'models' / ('shadow-window-' + hashlib_version(model['version']) + '.json')
        try:
            if not self._evaluation_state and state_path.exists():
                self._evaluation_state = json.loads(state_path.read_text())
            result = prospective_metrics(self.root / 'ledger.sqlite3', self.root.parent / 'models' / 'shadow.sqlite3',
                                         model, state=self._evaluation_state)
            self._live_metrics = result
            atomic_json(state_path, self._evaluation_state)
            atomic_json(self.root.parent / 'models' / 'prospective.json', result)
            self._next_evaluation = time.monotonic() + (5 if result.get('cursor',{}).get('has_more') else 30)
        except Exception as exc:
            self._live_metrics = {'version': model['version'], 'status':'error', 'reason': str(exc)}
            self._next_evaluation = time.monotonic()+30

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
            patch = {k: v for k, v in data['settings'].items() if k.startswith('model_training_') or
                     k in ('model_shadow_enabled', 'decision_mode', 'model_blend_weight')}
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
        status['replay'] = dict(self._replay_stats)
        status['model'].pop('weights', None)
        status['model'].pop('cohort_ids', None)
        atomic_json(self.root.parent / 'models' / 'dashboard.json', status)

    def _consume_replay(self) -> None:
        if self.root is None:
            return
        lock_path = self.root.parent / 'models' / 'replay.lock'
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            try:
                self._consume_replay_locked()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _consume_replay_locked(self) -> None:
        if self.root is None:
            return
        cursor_path = self.root.parent / 'models' / 'replay-cursor.json'
        if cursor_path.exists():
            persisted = json.loads(cursor_path.read_text())
            if isinstance(persisted, dict):
                self._cursor.update(persisted)
        # Prefer the newest journal first. In S3 mode old hourly chunks can be
        # multi-gigabyte; draining them in lexical order starves today's live
        # decisions and leaves the training input projection empty.
        paths = sorted(self.root.glob('live-replay*.jsonl.gz'),
                       key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
        for path in paths:
            offset = self._cursor.get(path.name, 0)
            if path.stat().st_size < offset:
                raise ValueError('永久决策日志被截断，不能跳过输入')
            if path.stat().st_size == offset:
                continue
            start_offset = offset
            batch_records = batch_inputs = members = 0
            with path.open('rb') as source:
                source.seek(offset)
                while members < REPLAY_MEMBERS_PER_CYCLE:
                    decoder = zlib.decompressobj(31)
                    linebuf = b''
                    while not decoder.eof:
                        compressed = source.read(64 * 1024)
                        if not compressed:
                            break
                        linebuf += decoder.decompress(compressed)
                    if not decoder.eof:
                        break  # Writer is finishing a gzip member; retry it.
                    end = source.tell() - len(decoder.unused_data)
                    if end <= offset:
                        raise ValueError('决策日志 gzip 成员游标未前进')
                    lines = linebuf.split(b'\n')
                    if lines[-1]:
                        raise ValueError('决策归档成员缺少完整记录边界')
                    for line in lines[:-1]:
                        if line:
                            row = json.loads(line)
                            batch_records += 1
                            before = len(self._inputs)
                            self.observe(row)
                            batch_inputs += len(self._inputs) - before
                            self._replay_stats['last_at'] = str(row.get('computed_at') or '')
                    offset = end
                    members += 1
                    source.seek(offset)
                    if len(self._inputs) >= 1000:
                        self._flush_shadow()
            if offset == start_offset:
                continue
            # Commit all decoded members before advancing the cursor. Replay
            # after a crash is idempotent through the inputs primary key.
            self._flush_shadow()
            self._cursor[path.name] = offset
            atomic_json(cursor_path, self._cursor)
            self._replay_stats.update(last_file=path.name, last_batch_records=batch_records,
                                      last_batch_inputs=batch_inputs,
                                      total_records=self._replay_stats['total_records'] + batch_records,
                                      total_inputs=self._replay_stats['total_inputs'] + batch_inputs)
            self._replay_stats['backlog_files'] = sum(
                1 for item in paths if self._cursor.get(item.name, 0) < item.stat().st_size)
            return  # Bound each loop to a finite batch; heartbeat/training continue.
        self._replay_stats.update(last_batch_records=0, last_batch_inputs=0, backlog_files=0)

    def _loop(self) -> None:
        next_check = 0.0
        next_stats = 0.0
        while not self._stop.is_set():
            try:
                self._reload_settings()
                self._read()
                self._consume_replay()
                self._flush_shadow()
                self._evaluate_shadow()
                self._publish()
                with self._lock:
                    cfg, root, process = self.cfg, self.root, self._process
                if root is None:
                    return
                if process is not None:
                    code = process.poll()
                    timed_out = False
                    if code is None and time.monotonic() - self._process_started > TRAINING_WALL_LIMIT_S:
                        timed_out = True
                        process.kill()
                        code = process.wait(timeout=3)
                    with self._lock:
                        self._read()
                        if code is not None:
                            if code and not self._retry_requested:
                                error = self._status.get('error') or self._process_exit_error(code, timed_out)
                                self._status.update(state='failed', error=error,
                                    process_exit_code=code,
                                    process_elapsed_s=round(time.monotonic() - self._process_started, 1),
                                    training_log=str(self._process_log_path or ''))
                            self._process = None
                            self._process_log_path = None
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
                            log_stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                            self._process_log_path = root.parent / 'models' / ('training-' + log_stamp + '.log')
                            with self._process_log_path.open('a', encoding='utf-8') as training_log:
                                training_log.write('training process started at ' + datetime.now(timezone.utc).isoformat() + '\n')
                                training_log.flush()
                                self._process = subprocess.Popen([sys.executable, '-u', '-m', 'service.model_training',
                                    str(root.resolve()), str(cfg.model_training_matches), str(cfg.model_training_cpu),
                                    str(cfg.model_training_memory_mb)], stdout=training_log,
                                    stderr=subprocess.STDOUT)
                            self._process_started = time.monotonic()
                            self._status = {**self._status, 'state': 'checking'}
                    next_check = time.monotonic() + 60
            except Exception as exc:
                with self._lock:
                    self._status.update(state='failed', error=str(exc))
                # Publish the worker heartbeat even when replay validation
                # fails. The dashboard must distinguish a running worker with
                # quarantined input from a worker that never started.
                try:
                    self._publish()
                except Exception:
                    pass
                next_check = time.monotonic() + 60
            self._stop.wait(1)
            if self._wake.is_set():
                self._wake.clear()
                next_check = 0
        try:
            self._flush_shadow()
        except Exception as exc:
            with self._lock:
                self._status.update(state='failed', error='模型输入保存失败: ' + str(exc))

    @staticmethod
    def _process_exit_error(code: int, timed_out: bool) -> str:
        if timed_out:
            return '训练进程超过 %s 秒运行上限' % TRAINING_WALL_LIMIT_S
        if code < 0:
            try:
                name = signal.Signals(-code).name
            except ValueError:
                name = 'UNKNOWN'
            return '训练进程被信号终止：%s (%s)' % (name, -code)
        return '训练进程异常退出，退出码 %s' % code


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
