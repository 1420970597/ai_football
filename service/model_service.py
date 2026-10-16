"""Independent model process: input extraction, inference, evaluation and training."""
from __future__ import annotations

from copy import deepcopy
from collections import deque
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
from typing import Iterator
import zlib

from core.learned_model import (FEATURES, approved_model, compatible_model,
                                model_compatibility_reason, predict)
from service.runtime_settings import RuntimeConfig, validated
from service.model_training import atomic_json

from store.training_data import (snapshot_inputs, write_inputs, write_predictions,
                                 prospective_metrics, prospective_prediction_rows,
                                 persist_prospective_predictions)
from store.training_projection import (prepare_postgres_schema, projection_target_from_env,
                                       close_persistent_writer)
from store.runtime_settings_db import load_runtime_settings
from store.replay_source import ReplaySource, ReplaySources
from store.disk_pressure import disk_pressure

REPLAY_MEMBERS_PER_CYCLE = 16
REPLAY_CYCLE_BUDGET_S = .25
REPLAY_POLL_S = .05
REPLAY_IDLE_S = 1.0
REPLAY_GAP_RECHECK_S = 300.0
REPLAY_NEWEST_BATCHES = max(1, int(os.environ.get('MODEL_INPUT_REPLAY_NEWEST_BATCHES', '4')))
LIVE_MEMBERS_PER_CYCLE = 16
LIVE_CYCLE_BUDGET_S = .1
LIVE_RECOVERY_MEMBERS_PER_CYCLE = 64
LIVE_RECOVERY_CYCLE_BUDGET_S = .25
LIVE_NEWEST_CYCLES_PER_OLD = 4
LIVE_POLL_S = .05
LIVE_TAIL_SCAN_BYTES = 4 * 1024 * 1024
GZIP_MEMBER_MAX_BYTES = 64 * 1024 * 1024
PERFORMANCE_SAMPLE_LIMIT = 512
PERSIST_BATCH_FILES = 64
PERSIST_BATCH_PREDICTIONS = 8192
PERSIST_BATCH_BYTES = 8 * 1024 * 1024
LIVE_OUTBOX_MAX_BYTES = 1024 * 1024 * 1024
TRAINING_WALL_LIMIT_S = int(os.environ.get('MODEL_TRAINING_WALL_LIMIT_S', '3600'))
TRAINING_CHECK_INTERVAL_S = 60
HISTORICAL_SHADOW_EVALUATION_INTERVAL_S = 60


def hashlib_version(version: str) -> str:
    return hashlib.sha256(str(version).encode()).hexdigest()[:24]


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path,os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def journal_members(path: Path | Any, offset: int, *, limit: int,
                    budget_s: float) -> Iterator[tuple[int,list[dict[str,Any]]]]:
    """One gzip parser shared by historical inputs and the live tail."""
    deadline = time.monotonic()+budget_s
    managed = path.open('rb') if isinstance(path, Path) else None
    source: Any = managed or path
    try:
        source.seek(offset)
        for _ in range(limit):
            if time.monotonic() >= deadline:
                return
            decoder = zlib.decompressobj(31)
            linebuf = bytearray()
            while not decoder.eof:
                compressed = source.read(64*1024)
                if not compressed:
                    return  # Partial member remains retryable at same cursor.
                linebuf.extend(decoder.decompress(compressed,GZIP_MEMBER_MAX_BYTES-len(linebuf)+1))
                if len(linebuf) > GZIP_MEMBER_MAX_BYTES or decoder.unconsumed_tail:
                    raise ValueError('决策日志成员超过解码内存上限')
            end = source.tell()-len(decoder.unused_data)
            if end <= offset:
                raise ValueError('决策日志 gzip 成员游标未前进')
            lines = linebuf.split(b'\n')
            if lines[-1]:
                raise ValueError('决策归档成员缺少完整记录边界')
            yield end,[json.loads(line) for line in lines[:-1] if line]
            offset = end
            source.seek(offset)
    finally:
        if managed is not None:
            managed.close()


def journal_tail_boundary(path: Path) -> int:
    """Find a complete member boundary without reading a gigabyte journal."""
    size = path.stat().st_size
    if not size:
        return 0
    start = max(0,size-LIVE_TAIL_SCAN_BYTES)
    with path.open('rb') as source:
        source.seek(start)
        tail = source.read()
    position = len(tail)
    while True:
        position = tail.rfind(b'\x1f\x8b\x08',0,position)
        if position < 0:
            raise ValueError('实时日志尾部未找到安全 gzip 边界，历史输入回放仍保留')
        try:
            header = tail[position:position+10]
            if len(header)<10 or header[3] != 0:
                # HistoryJournal uses the standard 10-byte, flags=0 header.
                # A coincidental magic sequence in deflate bytes is not a
                # trustworthy boundary, especially for partial trailing data.
                continue
            decoder = zlib.decompressobj(31)
            decoder.decompress(tail[position:],GZIP_MEMBER_MAX_BYTES+1)
            if decoder.unconsumed_tail:
                raise ValueError('实时日志成员超过解码内存上限')
            if decoder.eof:
                return start+len(tail)-len(decoder.unused_data)
            # Latest member is still being appended: begin at its header once
            # complete, rather than seeking into the middle of compressed data.
            return start+position
        except zlib.error:
            continue


def latency_percentiles(values: list[float]) -> dict[str,float | None]:
    ordered = sorted(values)
    return {f'p{p}':ordered[min(len(ordered)-1,int((len(ordered)-1)*p/100))] if ordered else None
            for p in (50,95,99)}


class ModelService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._replay_thread: threading.Thread | None = None
        self._live_thread: threading.Thread | None = None
        self._persist_thread: threading.Thread | None = None
        self._publish_lock = threading.Lock()
        self._persist_ready = threading.Event()
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
        self._next_historical_evaluation = 0.0
        self._historical_evaluation: dict[str, Any] = {'versions': 0, 'evaluated_versions': 0}
        self._historical_version = ''
        self._storage: dict[str, Any] = {}
        self._shadow: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._inputs: list[dict[str, Any]] = []
        self._live_metrics: dict[str, Any] = {}
        self._read_times: dict[str, tuple[int, int, int]] = {}
        self._process_started = 0.0
        self._stats_process: subprocess.Popen[Any] | None = None
        self._cursor: dict[str, int] = {}
        self._replay_sources: ReplaySources | None = None
        self._replay_source_meta: dict[str, dict[str, Any]] = {}
        self._replay_gaps: dict[str, dict[str, Any]] = {}
        self._replay_gap_probes: dict[str, tuple[dict[str, Any], int, float]] = {}
        self._live_cursor: dict[str, int] = {}
        self._live_inodes: dict[str,list[int]] = {}
        self._live_stats: dict[str,Any] = {'state':'not_started'}
        self._performance_samples: deque[dict[str,float]] = deque(maxlen=PERFORMANCE_SAMPLE_LIMIT)
        self._persist_samples: deque[float] = deque(maxlen=PERFORMANCE_SAMPLE_LIMIT)
        self._durable_samples: deque[float] = deque(maxlen=PERFORMANCE_SAMPLE_LIMIT)
        self._confirmation_samples: deque[float] = deque(maxlen=PERFORMANCE_SAMPLE_LIMIT)
        self._settings_mtime = 0
        self._settings_version: int | None = None
        self._next_settings_reload = 0.
        self._retry_requested = False
        self._replay_stats: dict[str, Any] = {
            'last_file': '', 'last_at': '', 'last_batch_records': 0,
            'last_batch_inputs': 0, 'total_records': 0, 'total_inputs': 0,
            'backlog_files': 0,
        }
        self._replay_newest_batches = 0
        self._replay_older_rotation = 0

    def bind(self, root: str | None) -> None:
        self.stop()
        with self._lock:
            self.root = Path(root) if root else None
            self._model = {}
            self._shadow_model = {}
            self._shadow_info = {}
            self._candidate_cache = {}
            self._evaluation_state = {}
            self._next_historical_evaluation = 0.
            self._historical_evaluation = {'versions': 0, 'evaluated_versions': 0}
            self._historical_version = ''
            self._read_times.clear()
            self._live_metrics.clear()
            self._shadow.clear()
            self._inputs.clear()
            self._storage = {}
            self._status = {'state': 'not_started'}
            self._cursor = {}
            self._replay_newest_batches = 0
            self._replay_older_rotation = 0
            self._replay_sources = ReplaySources(self.root) if self.root else None
            self._replay_source_meta = {}
            self._replay_gaps = {}
            self._replay_gap_probes = {}
            self._live_cursor = {}
            self._live_inodes = {}
            self._live_stats = {'state':'not_started'}
            self._performance_samples.clear()
            self._persist_samples.clear()
            self._durable_samples.clear()
            self._confirmation_samples.clear()
            self._settings_version = None
            self._next_settings_reload = 0.
            self._persist_ready.clear()
            self._replay_stats = {
                'last_file': '', 'last_at': '', 'last_batch_records': 0,
                'last_batch_inputs': 0, 'total_records': 0, 'total_inputs': 0,
                'backlog_files': 0, 'gap_files': 0,
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
        with self._lock:
            self._read_metadata()

    def _read_metadata(self) -> None:
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
                        if not approved_model(data):
                            self._model = {}
                            raise ValueError(model_compatibility_reason(data, require_approval=True))
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
                    raise ValueError(model_compatibility_reason(data) if isinstance(data, dict) else '无效模型元数据')
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
            self._performance_samples.clear()
            self._persist_samples.clear()
            self._durable_samples.clear()
            self._confirmation_samples.clear()
        self._shadow_model = chosen
        self._shadow_info = {**self._shadow_info, 'available': bool(chosen),
            'version': chosen.get('version'), 'artifact_kind': chosen.get('artifact_kind'),
            'approved': bool((chosen.get('promotion') or {}).get('approved')),
            'updated_at': chosen.get('updated_at'), 'available_at': chosen.get('updated_at'),
            'parameter_count': chosen.get('parameter_count'), 'architecture': chosen.get('architecture'),
            'reason': '' if chosen else reason}

    def _decision_route_status(self) -> tuple[str, str]:
        """Report live-route readiness separately from the worker heartbeat.

        Candidate artifacts are useful for shadow evaluation, but the live
        calculator may only load an approved ``active.json``.  Keeping this
        distinction in the dashboard prevents a healthy worker from being
        reported as stopped when only the selected decision route is blocked.
        """
        if self.cfg.decision_mode == 'algorithm':
            return 'ready', ''
        promotion = self._model.get('promotion') if isinstance(self._model, dict) else None
        if approved_model(self._model):
            return 'ready', ''
        reasons: list[str] = []
        if isinstance(promotion, dict):
            reasons.extend(str(item) for item in promotion.get('reasons') or [] if item)
        shadow_promotion = self._shadow_model.get('promotion') if isinstance(self._shadow_model, dict) else None
        if isinstance(shadow_promotion, dict):
            reasons.extend(str(item) for item in shadow_promotion.get('reasons') or [] if item)
        reasons.extend(str(item) for item in self._status.get('promotion_reasons') or [] if item)
        if reasons:
            return 'unavailable', '候选模型未通过晋级门槛：' + '；'.join(dict.fromkeys(reasons))
        if self._status.get('state') == 'candidate' or self._shadow_model.get('artifact_kind') == 'candidate':
            return 'unavailable', '已有候选模型，但尚未通过晋级门槛'
        return 'unavailable', '暂无通过晋级门槛的生产模型'

    def status(self) -> dict[str, Any]:
        with self._lock:
            model = deepcopy({k: v for k, v in self._model.items() if k not in ('weights', 'cohort_ids')})
            status = deepcopy(self._status)
            prospective = deepcopy(self._live_metrics) if self._live_metrics.get('version') == self._shadow_model.get('version') else {}
            shadow = deepcopy(self._shadow_info)
            shadow['enabled'] = self.cfg.model_shadow_enabled
            shadow['live'] = dict(self._live_stats)
            shadow['historical_evaluation'] = deepcopy(self._historical_evaluation)
            shadow['performance'] = {'scope':'live_lane_recent_process_samples','sample_limit':PERFORMANCE_SAMPLE_LIMIT,
                'version':self._shadow_model.get('version'),'samples':len(self._performance_samples),
                'persist_batches':len(self._persist_samples),'updated_at':datetime.now(timezone.utc).isoformat(),
                **{key:latency_percentiles([sample[key] for sample in self._performance_samples])
                   for key in ('compute_ms','queue_ms','decision_to_prediction_ms')},
                'prediction_persist_ms':latency_percentiles(list(self._persist_samples)),
                'decision_to_durable_ms':latency_percentiles(list(self._durable_samples)),
                'decision_to_pg_confirmed_ms':latency_percentiles(list(self._confirmation_samples)),
                'note':'本进程最近512个决策/提交样本；计算与生成只计实时lane，排队始于computed_at；durable为本机outbox落盘，PG确认含持久队列等待与恢复，持久事务含赛果校验及提交；各阶段分开展示'}
            if not self.cfg.model_shadow_enabled:
                shadow['reason'] = '影子预测记录已关闭；展示已有版本窗口，未生成新预测'
            if self._shadow_model:
                shadow['model'] = {k: deepcopy(v) for k,v in self._shadow_model.items()
                                   if k not in ('weights', 'cohort_ids')}
            if not self.cfg.model_training_enabled:
                status['state'] = 'disabled'
            decision_status, decision_reason = self._decision_route_status()
            service_running = bool(self._thread and self._thread.is_alive())
            return {'model': model, 'training': status, 'storage': deepcopy(self._storage),
                    'settings': {'enabled': self.cfg.model_training_enabled, 'shadow_enabled': self.cfg.model_shadow_enabled,
                                 'decision_mode': self.cfg.decision_mode,
                                 'model_blend_weight': self.cfg.model_blend_weight,
                                 'cycle_matches': self.cfg.model_training_matches,
                                 'cpu_cores': self.cfg.model_training_cpu,
                                 'memory_mb': self.cfg.model_training_memory_mb},
                    'mode': 'shadow' if self.cfg.model_shadow_enabled else 'active', 'running': service_running,
                    'decision_mode': self.cfg.decision_mode,
                    'decision_model_available': approved_model(self._model),
                    'decision_mode_status': decision_status,
                    'decision_mode_reason': decision_reason,
                    'service_status': {'state': 'running' if service_running else 'stopped',
                                       'running': service_running,
                                       'training_running': bool(self._process and self._process.poll() is None),
                                       'inference_running': service_running},
                    'prospective': prospective, 'shadow': shadow,
                    'note': '独立模型服务负责训练与观察预测；实时决策模式独立选择纯算法、纯决策模型或混合。纯模型是当前盘口offset与算法特征训练模型的输出，不进行额外算法加权。模型或混合模式缺少已晋级模型时不可用。保留各算法、盘口、盘口线、方向和决策时点；按比赛分组、等权训练。走势特征冻结在决策输入截点。'}

    def observe(self, row: dict[str, Any], *, record_inputs: bool = True,
                predict_shadow: bool = True, realtime: bool = False) -> None:
        started_perf,started_wall = time.perf_counter(),time.time()
        state = row.get('input_state') or {}
        if not row.get('received_at_ms') and state.get('received_at_ms'):
            row = {**row, 'received_at_ms': state['received_at_ms']}
        inputs = snapshot_inputs(row)
        with self._lock:
            if self.root is None:
                return
            if record_inputs:
                self._inputs.extend(inputs)
            model = self._shadow_model
            if not predict_shadow or not inputs or not self.cfg.model_shadow_enabled or not model:
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
                if realtime:
                    self._performance_samples.append({'compute_ms':(time.perf_counter()-started_perf)*1000,
                        'queue_ms':max(0.,(started_wall-decision_at.timestamp())*1000),
                        'decision_to_prediction_ms':max(0.,(datetime.fromisoformat(predicted_at).timestamp()-decision_at.timestamp())*1000)})

    def _flush_shadow(self) -> None:
        # Compatibility entry point for direct observations/tests. Runtime
        # live and historical lanes call their separate flushers.
        self._flush_predictions()
        self._flush_inputs()

    def _flush_inputs(self) -> None:
        with self._lock:
            if self.root is None:
                return
            inputs = list(self._inputs)
            path = self.root.parent/'models'/'inputs.sqlite3'
        write_inputs(path,inputs)
        with self._lock:
            del self._inputs[:len(inputs)]

    def _flush_predictions(self, *, realtime: bool = False) -> None:
        with self._lock:
            if self.root is None:
                return
            pending = dict(self._shadow)
            path = self.root.parent / 'models' / 'shadow.sqlite3'
        # write_inputs/write_predictions select the configured projection DSN.
        # With no DSN they preserve the existing local SQLite files; with a PG
        # DSN no model data is written to local SQLite at all.
        if not pending:
            return
        started = time.perf_counter()
        prospective = prospective_prediction_rows(self.root / 'ledger.sqlite3', list(pending.values()))
        write_predictions(path, prospective)
        with self._lock:
            for key in pending:
                self._shadow.pop(key, None)
            self._shadow_info['predictions_written'] = self._shadow_info.get('predictions_written',0)+len(prospective)
            self._shadow_info['skipped_settled_replay'] = self._shadow_info.get('skipped_settled_replay',0)+len(pending)-len(prospective)
            self._shadow_info['predictions_written_scope'] = '本进程提交写入条数，包含幂等重放；窗口数量以prospective.counts为准'
            if realtime and prospective:
                self._persist_samples.append((time.perf_counter()-started)*1000)

    def _evaluate_shadow(self) -> None:
        with self._lock:
            if self.root is None or time.monotonic() < self._next_evaluation:
                return
            if not self._shadow_model:
                self._live_metrics = {'status': 'unavailable', 'reason': self._shadow_info.get('reason'),
                                      'version': None}
                return
            root = self.root
            model_snapshot = self._shadow_model
            state_snapshot = self._evaluation_state
        model = deepcopy(model_snapshot)
        state = deepcopy(state_snapshot)
        state_path = root.parent / 'models' / ('shadow-window-' + hashlib_version(model['version']) + '.json')
        try:
            if not state and state_path.exists():
                state = json.loads(state_path.read_text())
            # PG work may outlive a candidate switch. Work on an independent
            # snapshot so an old evaluation cannot mutate the new window.
            result = prospective_metrics(root / 'ledger.sqlite3', root.parent / 'models' / 'shadow.sqlite3',
                                         model, state=state)
            with self._lock:
                if self.root != root:
                    return
            # The large rolling window is version-specific; its serialization
            # must not block the live lane's metadata/prediction lock.
            atomic_json(state_path, state)
            self._persist_shadow_summary(root, model, result)
            with self._lock:
                if self.root != root or self._shadow_model.get('version') != model['version']:
                    return
                self._evaluation_state = state
                self._live_metrics = result
                self._next_evaluation = time.monotonic() + (5 if result.get('cursor',{}).get('has_more') else 30)
            # This compact summary identifies its version; readers discard a
            # previous version if a candidate changes during the file write.
            atomic_json(root.parent / 'models' / 'prospective.json', result)
        except Exception as exc:
            with self._lock:
                if self.root != root or self._shadow_model.get('version') != model['version']:
                    return
                self._live_metrics = {'version': model['version'], 'status':'error', 'reason': str(exc)}
                self._next_evaluation = time.monotonic()+30

    @staticmethod
    def _persist_shadow_summary(root: Path, model: dict[str, Any], result: dict[str, Any]) -> None:
        atomic_json(root.parent / 'models' / ('shadow-metrics-' + hashlib_version(model['version']) + '.json'), {
            **result, 'version': model['version'], 'summary_schema_version': 'versioned-shadow-metrics-v1',
            'data_version': model.get('data_version'),
            'validation_contract_version': model.get('validation_contract_version'),
            'evaluated_at': datetime.now(timezone.utc).isoformat()})

    def _evaluate_historical_shadow(self) -> None:
        """Refresh one compatible historical version per bounded cycle.

        A newer candidate does not stop settlement of predictions made by a
        prior candidate. Historical results have separate windows/summaries
        and never replace the current version displayed by the live route.
        """
        with self._lock:
            if self.root is None or time.monotonic() < self._next_historical_evaluation:
                return
            root, current_version = self.root, self._shadow_model.get('version')
            self._next_historical_evaluation = time.monotonic() + HISTORICAL_SHADOW_EVALUATION_INTERVAL_S
        model_root = root.parent / 'models'
        models: dict[str, dict[str, Any]] = {}
        for path in sorted(model_root.glob('candidate-*.json')) + sorted(model_root.glob('active-backup-*.json')):
            try:
                model = json.loads(path.read_text(encoding='utf-8'))
                if (not isinstance(model, dict) or not compatible_model(model)
                        or model.get('version') == current_version):
                    continue
                available = datetime.fromisoformat(model['updated_at'])
                if available.tzinfo is None or available > datetime.now(timezone.utc):
                    continue
                predict(model['weights'], [0.0] * len(FEATURES))
                models[str(model['version'])] = model
            except (OSError, ValueError, TypeError, KeyError):
                continue
        versions = sorted(models)
        summaries = sum((model_root / ('shadow-metrics-' + hashlib_version(version) + '.json')).exists()
                        for version in versions)
        with self._lock:
            self._historical_evaluation.update(versions=len(versions), evaluated_versions=summaries,
                interval_s=HISTORICAL_SHADOW_EVALUATION_INTERVAL_S,
                scope='one compatible historical version per cycle; pending outcomes remain pending')
        if not versions:
            return
        version = next((item for item in versions if item > self._historical_version), versions[0])
        self._historical_version = version
        state_path = model_root / ('shadow-window-' + hashlib_version(version) + '.json')
        try:
            state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
            result = prospective_metrics(root / 'ledger.sqlite3', model_root / 'shadow.sqlite3', models[version], state=state)
            atomic_json(state_path, state)
            self._persist_shadow_summary(root, models[version], result)
            with self._lock:
                self._historical_evaluation.update(last_version=version,
                    last_evaluated_at=datetime.now(timezone.utc).isoformat(),
                    last_status=result.get('status'), last_counts=deepcopy(result.get('counts', {})), error='')
        except Exception as exc:
            with self._lock:
                self._historical_evaluation.update(last_version=version, error=str(exc))

    def start(self) -> None:
        if self.root is None or self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._reload_settings()
        target = projection_target_from_env()
        if target is not None:
            with self._lock:
                if self._settings_version is None:
                    reason = self._status.get('settings_error') or '尚未读取PG权威设置'
                    raise RuntimeError('模型服务拒绝启动：首次PG设置加载失败：'+str(reason))
            prepare_postgres_schema(target)
        self._initialize_live_cursor()
        self._live_thread = threading.Thread(target=self._live_loop,name='model-live-shadow',daemon=True)
        self._persist_thread = threading.Thread(target=self._persist_loop,name='model-shadow-persist',daemon=True)
        self._replay_thread = threading.Thread(target=self._replay_loop, name='model-input-replay', daemon=True)
        self._thread = threading.Thread(target=self._loop, name='data-model', daemon=True)
        self._live_thread.start()
        self._persist_thread.start()
        self._replay_thread.start()
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
        if self._replay_thread and self._replay_thread.is_alive():
            self._replay_thread.join(timeout=5)
        if self._replay_thread and self._replay_thread.is_alive():
            raise ValueError('训练输入后台尚未停止，保留游标等待恢复')
        if self._live_thread and self._live_thread.is_alive():
            self._live_thread.join(timeout=5)
        if self._live_thread and self._live_thread.is_alive():
            raise ValueError('实时影子后台尚未停止，游标/outbox保留供重启恢复')
        if self._persist_thread and self._persist_thread.is_alive():
            self._persist_thread.join(timeout=5)
        if self._persist_thread and self._persist_thread.is_alive():
            raise ValueError('影子持久化后台尚未停止，durable outbox保留供重启恢复')
        if self._stats_process and self._stats_process.poll() is None:
            self._stats_process.terminate()
            try:
                self._stats_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._stats_process.kill()
                self._stats_process.wait(timeout=3)
        self._thread = None
        self._replay_thread = None
        self._live_thread = None
        self._persist_thread = None
        if self._replay_sources is not None:
            self._replay_sources.close()
            self._replay_sources = None
        self._flush_shadow()
        self._publish(False)

    def _reload_settings(self) -> None:
        if self.root is None:
            return
        if projection_target_from_env() is not None:
            if time.monotonic() < self._next_settings_reload:
                return
            self._next_settings_reload = time.monotonic()+5
            try:
                data = load_runtime_settings(None)
            except Exception as exc:
                with self._lock:
                    self._status['settings_error'] = str(exc)
                return  # Preserve the last applied PG configuration.
            with self._lock:
                if not data:
                    self._status['settings_error'] = 'PG权威设置不存在；不能使用默认模型配置'
                    return
                self._status.pop('settings_error',None)
            if data.get('version') == self._settings_version:
                return
            patch = {k:v for k,v in data['settings'].items() if k.startswith('model_training_') or
                     k in ('model_shadow_enabled','decision_mode','model_blend_weight')}
            self.configure(validated(self.cfg,patch))
            self._settings_version = data['version']
            return
        path = self.root.parent / 'runtime-settings.json'
        if path.exists() and path.stat().st_mtime_ns != self._settings_mtime:
            data = json.loads(path.read_text())
            patch = {k: v for k, v in data['settings'].items() if k.startswith('model_training_') or
                     k in ('model_shadow_enabled', 'decision_mode', 'model_blend_weight')}
            self.configure(validated(self.cfg, patch))
            self._settings_mtime = path.stat().st_mtime_ns

    def _publish(self, running: bool = True) -> None:
        with self._publish_lock:
            self._publish_status(running)

    def _publish_status(self, running: bool = True) -> None:
        if self.root is None:
            return
        self._read()
        status = self.status()
        status.update(running=running, inference_running=running,
                      training_running=bool(self._process and self._process.poll() is None),
                      heartbeat_at=datetime.now(timezone.utc).isoformat(),
                      process_id=os.getpid(), service='model-worker')
        with self._lock:
            status['replay'] = dict(self._replay_stats)
        processed = status['replay'].get('newest_processed_at')
        if processed:
            status['replay']['newest_lag_s'] = max(0., time.time()-datetime.fromisoformat(processed).timestamp())
        status.setdefault('storage', {})['disk_pressure'] = disk_pressure(self.root.parent)
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

    def _replay_state_paths(self) -> tuple[Path, Path, Path]:
        assert self.root is not None
        model_root = self.root.parent / 'models'
        return (model_root / 'replay-cursor.json',
                model_root / 'replay-source-meta.json',
                model_root / 'replay-gaps.json')

    @staticmethod
    def _source_descriptor(source: ReplaySource) -> dict[str, Any]:
        record = dict(source.record or {})
        return {
            'protocol': 1,
            'size': int(source.size),
            'mtime_ns': int(source.mtime_ns),
            'remote': bool(source.remote),
            'object_key': record.get('object_key', ''),
            'offset': int(record.get('offset', 0) or 0),
            'compressed': int(record.get('compressed', 0) or 0),
            'sha256': str(record.get('sha256', '')),
        }

    def _load_replay_state(self) -> None:
        cursor_path, meta_path, gap_path = self._replay_state_paths()
        try:
            data = json.loads(cursor_path.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                self._cursor.update({str(k): int(v) for k, v in data.items()})
        except (OSError, ValueError, TypeError):
            pass
        try:
            data = json.loads(meta_path.read_text(encoding='utf-8'))
            if isinstance(data, dict) and isinstance(data.get('sources'), dict):
                self._replay_source_meta = {
                    str(k): dict(v) for k, v in data['sources'].items()
                    if isinstance(v, dict)
                }
        except (OSError, ValueError, TypeError):
            pass
        try:
            data = json.loads(gap_path.read_text(encoding='utf-8'))
            if isinstance(data, dict) and isinstance(data.get('gaps'), dict):
                self._replay_gaps = {
                    str(k): dict(v) for k, v in data['gaps'].items()
                    if isinstance(v, dict)
                }
        except (OSError, ValueError, TypeError):
            pass

    def _save_replay_state(self, *, cursor: bool = False, metadata: bool = False,
                           gaps: bool = False) -> None:
        cursor_path, meta_path, gap_path = self._replay_state_paths()
        if cursor:
            atomic_json(cursor_path, self._cursor)
        if metadata:
            atomic_json(meta_path, {'protocol': 1, 'sources': self._replay_source_meta})
        if gaps:
            atomic_json(gap_path, {'protocol': 1, 'gaps': self._replay_gaps})

    def _record_replay_gap(self, source: ReplaySource, cursor: int, reason: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        previous = self._replay_gaps.get(source.name, {})
        self._replay_gaps[source.name] = {
            'source': self._source_descriptor(source),
            'cursor': int(cursor),
            'observed_size': int(source.size),
            'reason': reason,
            'first_at': previous.get('first_at', now),
            'last_at': now,
            'attempts': int(previous.get('attempts', 0) or 0) + 1,
            'frozen': True,
        }
        self._save_replay_state(gaps=True)
        self._replay_stats['gap_files'] = len(self._replay_gaps)

    def _consume_replay_locked(self) -> None:
        if self.root is None:
            return
        # A failed PG write retains this batch. Never decode/append the same
        # unconfirmed journal range repeatedly during a prolonged outage.
        if self._inputs:
            self._flush_inputs()
        self._load_replay_state()
        sources = self._replay_sources or ReplaySources(self.root)
        self._replay_sources = sources
        replay_sources = sources.list_sources()
        self._replay_stats.update(
            source_protocol=1,
            source_backend='s3' if sources.remote_available else 'local',
            remote_read_verified=sources.verify_remote_read() if sources.remote_available else False,
        )
        pending = []
        for source in replay_sources:
            offset = int(self._cursor.get(source.name, 0) or 0)
            descriptor = self._source_descriptor(source)
            if offset < 0 or source.size < offset:
                previous = self._replay_gap_probes.get(source.name)
                if (previous and previous[:2] == (descriptor, offset)
                        and time.monotonic() < previous[2]):
                    continue
            if offset < 0:
                self._record_replay_gap(source, offset, 'cursor 为负数')
                self._replay_gap_probes[source.name] = (descriptor, offset,
                    time.monotonic()+REPLAY_GAP_RECHECK_S)
                continue
            if source.size < offset:
                historical = sources.historical_sources(source.name, offset)
                if historical:
                    source = max(historical, key=lambda item: (item.size, item.mtime_ns))
                else:
                    self._record_replay_gap(source, offset, '远端源长度小于持久 cursor，未找到可恢复历史 pack')
                    self._replay_gap_probes[source.name] = (descriptor, offset,
                        time.monotonic()+REPLAY_GAP_RECHECK_S)
                    continue
            self._replay_gap_probes.pop(source.name, None)
            if source.size == offset:
                self._replay_source_meta[source.name] = self._source_descriptor(source)
                continue
            pending.append(source)
        if not pending:
            self._save_replay_state(metadata=True, gaps=True)
            with self._lock:
                self._replay_stats.update(last_batch_records=0, last_batch_inputs=0,
                    backlog_files=0, backlog_bytes=0, gap_files=len(self._replay_gaps))
            return
        # Keep current input moving while granting every older journal a turn.
        # A continuously growing newest file must not starve settled matches.
        if len(pending)>1 and self._replay_newest_batches >= REPLAY_NEWEST_BATCHES:
            source = pending[1+self._replay_older_rotation % (len(pending)-1)]
            self._replay_older_rotation += 1
            self._replay_newest_batches = 0
        else:
            source = pending[0]
            self._replay_newest_batches += 1
        offset = int(self._cursor.get(source.name, 0) or 0)
        start_offset = offset
        batch_records = batch_inputs = 0
        virtual_records = unqualified_real_records = unknown_records = 0
        newest_at = ''
        with sources.open(source, offset) as stream:
            for end, rows in journal_members(stream, offset, limit=REPLAY_MEMBERS_PER_CYCLE,
                                             budget_s=REPLAY_CYCLE_BUDGET_S):
                for row in rows:
                    batch_records += 1
                    before = len(self._inputs)
                    self.observe(row, predict_shadow=False)
                    added = len(self._inputs) - before
                    batch_inputs += added
                    kind = row.get('competition_type')
                    virtual_records += kind == 'virtual'
                    unknown_records += kind not in ('real', 'virtual')
                    unqualified_real_records += kind == 'real' and not added
                    newest_at = max(newest_at, str(row.get('computed_at') or ''))
                offset = end
        if offset == start_offset:
            return
        # Commit all decoded members before advancing the cursor. Replay
        # after a crash is idempotent through the inputs primary key.
        self._flush_inputs()
        self._cursor[source.name] = offset
        self._replay_source_meta[source.name] = self._source_descriptor(source)
        self._replay_gaps.pop(source.name, None)
        self._save_replay_state(cursor=True, metadata=True, gaps=True)
        with self._lock:
            self._replay_stats.update(last_file=source.name, last_at=newest_at,
                last_batch_records=batch_records, last_batch_inputs=batch_inputs,
                last_batch_virtual_records=virtual_records,
                last_batch_unqualified_real_records=unqualified_real_records,
                last_batch_unknown_records=unknown_records,
                committed_at=datetime.now(timezone.utc).isoformat(),
                total_records=self._replay_stats['total_records']+batch_records,
                total_inputs=self._replay_stats['total_inputs']+batch_inputs,
                counts_scope='current_process_committed_idempotent_rows',
                gap_files=len(self._replay_gaps),
                backlog_files=sum(self._cursor.get(item.name,0)<item.size for item in pending),
                backlog_bytes=sum(max(0,item.size-self._cursor.get(item.name,0)) for item in pending))
            if replay_sources and source.name == replay_sources[0].name:
                self._replay_stats['newest_processed_at'] = newest_at

    def _replay_loop(self) -> None:
        """One bounded input writer, independent of settlement and training IO."""
        try:
            while not self._stop.is_set():
                previous = self._replay_stats.get('total_records', 0)
                delay = REPLAY_IDLE_S
                try:
                    self._consume_replay()
                    progressed = self._replay_stats.get('total_records', 0) != previous
                    with self._lock:
                        self._replay_stats.update(state='processing' if progressed else 'waiting',
                                                  running=True, error='')
                    delay = REPLAY_POLL_S if progressed else REPLAY_IDLE_S
                except Exception as exc:
                    with self._lock:
                        self._replay_stats.update(state='error', running=True, error=str(exc))
                self._stop.wait(delay)
        finally:
            with self._lock:
                self._replay_stats['running'] = False

    def _initialize_live_cursor(self) -> None:
        if self.root is None:
            return
        lock_path = self.root.parent/'models'/'replay.lock'
        lock_path.parent.mkdir(parents=True,exist_ok=True)
        with lock_path.open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_SH)
            try:
                self._initialize_live_cursor_locked()
            finally:
                fcntl.flock(lock,fcntl.LOCK_UN)

    def _save_live_checkpoint(self) -> None:
        if self.root is not None:
            atomic_json(self.root.parent/'models'/'live-shadow-cursor.json',
                        {'schema_version':2,'cursor':self._live_cursor,'inodes':self._live_inodes})

    def _commit_live_batch_cursor(self, batch: dict[str,Any]) -> None:
        if self.root is None:
            return
        with (self.root.parent/'models'/'replay.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_SH)
            try:
                for name,offset in batch['cursor'].items():
                    path = self.root/name
                    expected = batch.get('inodes',{}).get(name)
                    if not path.exists():
                        self._live_stats['detached_outbox_cursors'] = self._live_stats.get('detached_outbox_cursors',0)+1
                        continue
                    stat = path.stat()
                    actual = [stat.st_dev,stat.st_ino]
                    if ((expected is not None and expected != actual) or
                            (expected is None and self._live_stats.get('repair_tail_resets',0))):
                        # Original probabilities are valid independently of a
                        # journal repair, but an old inode's byte offset cannot
                        # overwrite the new file's safe initialization boundary.
                        self._live_cursor[name] = journal_tail_boundary(path)
                        self._live_stats['outbox_cursor_inode_mismatches'] = self._live_stats.get('outbox_cursor_inode_mismatches',0)+1
                    else:
                        self._live_cursor[name] = int(offset)
                    self._live_inodes[name] = actual
                self._save_live_checkpoint()
            finally:
                fcntl.flock(lock,fcntl.LOCK_UN)

    def _initialize_live_cursor_locked(self) -> None:
        if self.root is None:
            return
        cursor_path = self.root.parent/'models'/'live-shadow-cursor.json'
        if cursor_path.exists():
            data = json.loads(cursor_path.read_text())
            self._live_cursor = {str(key):int(value) for key,value in data.get('cursor',data).items()}
            self._live_inodes = data.get('inodes',{})
            self._register_live_journals(list(self.root.glob('live-replay*.jsonl.gz')))
            self._save_live_checkpoint()
            self._live_stats.update(state='recovering',cursor_basis='persisted_live_cursor',
                                    recovery_until=time.time(),recovered_records=0)
        else:
            paths = list(self.root.glob('live-replay*.jsonl.gz'))
            self._live_cursor = {path.name:journal_tail_boundary(path) for path in paths}
            self._live_inodes = {path.name:[path.stat().st_dev,path.stat().st_ino] for path in paths}
            self._save_live_checkpoint()
            self._live_stats.update(state='following',cursor_basis='initial_complete_tail',
                skipped_existing_journals=len(paths),skipped_existing_bytes=sum(self._live_cursor.values()),
                skipped_existing_scope='仅跳过历史影子补录，历史inputs独立游标继续永久入库',recovered_records=0)
        self._live_stats.setdefault('records',0)

    def _register_live_journals(self, paths: list[Path]) -> None:
        def hour(name: str) -> str | None:
            suffix = name.removeprefix('live-replay.').removesuffix('.jsonl.gz')
            return suffix if len(suffix)==10 and suffix.isdigit() else None
        known_hours = [value for name in self._live_cursor if (value:=hour(name)) is not None]
        latest_known = max(known_hours,default='')
        for path in paths:
            stat = path.stat()
            signature = [stat.st_dev,stat.st_ino]
            previous = self._live_inodes.get(path.name)
            if previous is not None and previous != signature:
                # Maintenance atomically replaced the gzip inode. Its previous
                # byte offsets no longer describe this file; historical repair
                # owns full inputs, while shadow resumes at a safe tail and
                # openly excludes the repaired historical gap.
                boundary = journal_tail_boundary(path)
                self._live_stats['repair_tail_resets'] = self._live_stats.get('repair_tail_resets',0)+1
                self._live_stats['repair_reset_scope'] = 'gzip替换后只跟随安全尾部，修复历史inputs完整重放；未补写旧影子为实时'
                self._live_cursor[path.name] = boundary
            self._live_inodes[path.name] = signature
            if path.name not in self._live_cursor:
                # Hourly suffix names order chronologically. Journals created
                # while stopped/PG retrying recover from start; older imported
                # historical files retain the initial-tail contract.
                path_hour = hour(path.name)
                recover = not latest_known or path_hour is None or path_hour > latest_known
                self._live_cursor[path.name] = 0 if recover else journal_tail_boundary(path)

    def _consume_live_shadow(self, *, enqueue_only: bool = False) -> None:
        if self.root is None:
            return
        model_root = self.root.parent/'models'
        outbox = model_root/'live-shadow-outbox.json'
        queue = model_root/'live-shadow-outbox'
        if enqueue_only:
            queue_existed = queue.exists()
            queue.mkdir(exist_ok=True)
            if not queue_existed:
                fsync_directory(model_root)
            # Recover the former single-batch outbox without recomputing it.
            if outbox.exists():
                legacy = json.loads(outbox.read_text())
                atomic_json(queue/('00000000000000000000-'+str(len(legacy['predictions']))+'.json'),legacy)
                fsync_directory(queue)
                self._commit_live_batch_cursor(legacy)
                outbox.unlink()
            queued = sorted(queue.glob('*.json'))
            # Enqueue/checkpoint crash: newest durable batch owns the source
            # cursor even if PG has not confirmed any pending predictions.
            if queued and not self._live_stats.get('outbox_recovered'):
                # A prior rename may have succeeded while its directory fsync
                # failed. Confirm durability before acknowledging its cursor.
                fsync_directory(queue)
                for pending_path in queued:
                    pending_batch = json.loads(pending_path.read_text())
                    advances = any(offset > self._live_cursor.get(name,0)
                        for name,offset in pending_batch['cursor'].items())
                    if advances:
                        self._commit_live_batch_cursor(pending_batch)
                self._live_stats['outbox_recovered'] = True
            self._persist_ready.set()
            queue_stats = self._outbox_stats(queue)
            queue_bytes = queue_stats['outbox_bytes']
            self._live_stats.update(**queue_stats,outbox_limit_bytes=LIVE_OUTBOX_MAX_BYTES)
            if queue_bytes >= LIVE_OUTBOX_MAX_BYTES:
                raise ValueError('影子durable outbox达到磁盘预算；保留源日志与全部预测，等待PG恢复')
        elif outbox.exists():
            batch = json.loads(outbox.read_text())
            with self._lock:
                for item in batch['predictions']:
                    key = tuple([item['version'], *[item[k] for k in ('match_id','at','algorithm','market','line','outcome')]])
                    self._shadow[key] = item
            self._flush_predictions()
            self._commit_live_batch_cursor(batch)
            outbox.unlink()
            self._live_stats['recovered_outbox_predictions'] = self._live_stats.get('recovered_outbox_predictions',0)+len(batch['predictions'])
        lock_path = model_root/'replay.lock'
        with lock_path.open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_SH)
            try:
                batch_cursor,predictions,realtime_batch,records = self._read_live_batch_locked()
                batch_inodes = {name:self._live_inodes[name] for name in batch_cursor}
            finally:
                fcntl.flock(lock,fcntl.LOCK_UN)
        if not batch_cursor:
            return
        # The shared repair lock is released before label/PG IO.
        batch = {'cursor':batch_cursor,'inodes':batch_inodes,'predictions':predictions}
        if enqueue_only:
            batch.update(realtime=realtime_batch,enqueued_at=datetime.now(timezone.utc).isoformat())
            destination = queue/(str(time.time_ns()).zfill(20)+'-'+str(len(predictions))+'.json')
            atomic_json(destination,batch)
            fsync_directory(queue)
            self._commit_live_batch_cursor(batch)
            with self._lock:
                durable_at = time.time()
                for item in predictions:
                    key = tuple([item['version'], *[item[k] for k in ('match_id','at','algorithm','market','line','outcome')]])
                    self._shadow.pop(key,None)
                if realtime_batch and predictions:
                    self._durable_samples.extend(max(0.,(durable_at-datetime.fromisoformat(
                        item['at']).timestamp())*1000) for item in predictions
                        if item['version'] == self._shadow_model.get('version'))
            self._live_stats.update(last_enqueued_batch=destination.name,state='following',
                                    records=self._live_stats['records']+records,last_error='')
            return
        atomic_json(outbox,batch)
        self._flush_predictions(realtime=realtime_batch)
        self._commit_live_batch_cursor(batch)
        outbox.unlink()
        self._live_stats.update(state='following',records=self._live_stats['records']+records,last_error='')

    def _outbox_stats(self, queue: Path) -> dict[str,Any]:
        paths = sorted(queue.glob('*.json'))
        total_bytes = count = predictions = 0
        oldest = None
        for path in paths:
            try:
                size = path.stat().st_size
                total_bytes += size
                count += 1
                predictions += int(path.stem.split('-')[-1])
                if oldest is None:
                    oldest = json.loads(path.read_text()).get('enqueued_at')
            except FileNotFoundError:
                continue  # The independent consumer confirmed this batch.
        return {'outbox_batches':count,'outbox_bytes':total_bytes,'outbox_predictions':predictions,
                'outbox_oldest_at':oldest,'outbox_oldest_wait_ms':max(0.,(time.time()-
                    datetime.fromisoformat(oldest).timestamp())*1000) if oldest else 0.}

    def _persist_live_outbox(self) -> None:
        if self.root is None:
            return
        queue = self.root.parent/'models'/'live-shadow-outbox'
        selected: list[Path] = []
        rows: list[dict[str,Any]] = []
        realtime = False
        selected_bytes = 0
        decode_deadline = time.monotonic()+.1
        for path in sorted(queue.glob('*.json'))[:PERSIST_BATCH_FILES]:
            if selected and time.monotonic() >= decode_deadline:
                break
            size = path.stat().st_size
            batch = json.loads(path.read_text())
            if selected and (len(rows)+len(batch['predictions']) > PERSIST_BATCH_PREDICTIONS or
                             selected_bytes+size > PERSIST_BATCH_BYTES):
                break
            selected.append(path)
            selected_bytes += size
            rows.extend(batch['predictions'])
            realtime = realtime or bool(batch.get('realtime'))
        if not selected:
            with self._lock:
                self._live_stats.update(persist_state='idle')
            return
        with self._lock:
            self._live_stats.update(persist_state='persisting')
        started = time.perf_counter()
        confirmed = persist_prospective_predictions(self.root/'ledger.sqlite3',
            self.root.parent/'models'/'shadow.sqlite3',rows)
        written = len(confirmed)
        # Commit succeeds before deleting any durable batch. A crash or retry
        # reuses original probabilities and timestamps; PG identity is idempotent.
        for path in selected:
            path.unlink()
        remaining_stats = self._outbox_stats(queue)
        with self._lock:
            self._shadow_info['predictions_written'] = self._shadow_info.get('predictions_written',0)+written
            self._shadow_info['skipped_settled_replay'] = self._shadow_info.get('skipped_settled_replay',0)+len(rows)-written
            current_confirmed = [item for item in confirmed if item['version'] == self._shadow_model.get('version')]
            if realtime and current_confirmed:
                self._persist_samples.append((time.perf_counter()-started)*1000)
            confirmed_at = time.time()
            self._confirmation_samples.extend(max(0.,(confirmed_at-datetime.fromisoformat(
                item['at']).timestamp())*1000) for item in current_confirmed)
            self._live_stats.update(persist_state='pending' if remaining_stats['outbox_batches'] else 'idle',persist_error='',
                **remaining_stats,
                last_persist_batch_files=len(selected),last_persist_predictions=written)

    def _persist_loop(self) -> None:
        try:
            while not self._stop.is_set():
                if not self._persist_ready.wait(.05):
                    continue
                try:
                    self._persist_live_outbox()
                except Exception as exc:
                    with self._lock:
                        self._live_stats.update(persist_state='error',persist_error=str(exc))
                    self._stop.wait(1)
                self._stop.wait(LIVE_POLL_S)
        finally:
            close_persistent_writer()

    def _read_live_batch_locked(self) -> tuple[dict[str,int],list[dict[str,Any]],bool,int]:
        if self.root is None:
            return {},[],False,0
        paths = sorted(self.root.glob('live-replay*.jsonl.gz'),key=lambda p:(p.stat().st_mtime_ns,p.name),reverse=True)
        if not paths:
            return {},[],False,0
        before_cursor,before_inodes = dict(self._live_cursor),dict(self._live_inodes)
        self._register_live_journals(paths)
        old_pending = [path for path in paths[1:] if path.name in self._live_cursor and
                       self._live_cursor[path.name]<path.stat().st_size]
        newest_pending = self._live_cursor.get(paths[0].name,0)<paths[0].stat().st_size
        newest_cycles = int(self._live_stats.get('newest_cycles',0))
        # Bound recovery work, but allocate four newest batches for every
        # older suffix batch. Once newest catches up, drain old continuously.
        if old_pending and (not newest_pending or newest_cycles >= LIVE_NEWEST_CYCLES_PER_OLD):
            rotation = int(self._live_stats.get('old_suffix_rotation',0))
            path = old_pending[rotation % len(old_pending)]
            self._live_stats['old_suffix_rotation'] = rotation+1
            self._live_stats['newest_cycles'] = 0
        else:
            path = paths[0]
            self._live_stats['newest_cycles'] = newest_cycles+1 if newest_pending else 0
        self._live_stats['last_poll_file'] = path.name
        offset = self._live_cursor.get(path.name,0)
        if path.stat().st_size < offset:
            raise ValueError('实时日志被截断，独立live cursor不能跳过未知边界')
        next_offset = offset
        records = 0
        realtime_batch = True
        recovering = bool(old_pending or self._live_stats.get('catching_up'))
        # A journal suffix larger than one normal batch is visible backlog.
        # Once it drains to the current tail, normal polling resumes.
        members_limit = LIVE_RECOVERY_MEMBERS_PER_CYCLE if recovering else LIVE_MEMBERS_PER_CYCLE
        budget = LIVE_RECOVERY_CYCLE_BUDGET_S if recovering else LIVE_CYCLE_BUDGET_S
        for end,rows in journal_members(path,offset,limit=members_limit,budget_s=budget):
            for row in rows:
                decision_at = datetime.fromisoformat(row['computed_at'])
                recovered = path != paths[0] or decision_at.timestamp() <= float(self._live_stats.get('recovery_until',0))
                realtime_batch = realtime_batch and not recovered
                self.observe(row,record_inputs=False,realtime=not recovered)
                records += 1
                if recovered:
                    self._live_stats['recovered_records'] += 1
                self._live_stats['last_decision_at'] = row['computed_at']
            next_offset = end
        if next_offset == offset:
            self._live_stats['catching_up'] = False
            if self._live_cursor != before_cursor or self._live_inodes != before_inodes:
                self._save_live_checkpoint()
            return {},[],False,0
        with self._lock:
            predictions = list(self._shadow.values())
        backlog = max(0,path.stat().st_size-next_offset)
        self._live_stats.update(last_file=path.name,backlog_bytes=backlog,
            catching_up=bool(backlog or (old_pending and path == paths[0])))
        return {path.name:next_offset},predictions,realtime_batch,records

    def _live_loop(self) -> None:
        next_publish = 0.
        while not self._stop.is_set():
            try:
                self._consume_live_shadow(enqueue_only=True)
            except Exception as exc:
                self._live_stats.update(state='error',last_error=str(exc))
                self._stop.wait(1)  # Failed PG/outbox operations do not hot-loop.
            if time.monotonic() >= next_publish:
                try:
                    self._publish()
                except Exception as exc:
                    self._live_stats['last_error'] = str(exc)
                next_publish = time.monotonic()+1
            self._stop.wait(0 if self._live_stats.get('catching_up') else LIVE_POLL_S)

    def _loop(self) -> None:
        next_check = 0.0
        next_stats = 0.0
        while not self._stop.is_set():
            try:
                self._reload_settings()
                self._read()
                self._evaluate_shadow()
                self._evaluate_historical_shadow()
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
                            next_check = 0 if self._retry_requested else time.monotonic() + TRAINING_CHECK_INTERVAL_S
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
                        self._start_training_check(root, cfg)
                    next_check = time.monotonic() + TRAINING_CHECK_INTERVAL_S
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
                next_check = time.monotonic() + TRAINING_CHECK_INTERVAL_S
            self._stop.wait(1)
            if self._wake.is_set():
                self._wake.clear()
                next_check = 0

    def _start_training_check(self, root: Path, cfg: RuntimeConfig) -> None:
        """Schedule learning using its own switch, independent of execution.

        The child checks a cheap qualified-match cohort before loading any
        feature dataset. Neither betting, decision mode nor shadow recording
        gates the training clock.
        """
        if not cfg.model_training_enabled or self._process is not None or self._stop.is_set():
            return
        pressure = disk_pressure(root.parent)
        if pressure.get('pause_optional_writes', True):
            self._status.update(state='waiting_for_disk', resource_wait_reason=pressure['reason'],
                                disk_pressure=pressure)
            return
        self._status.pop('resource_wait_reason', None)
        model_root = root.parent / 'models'
        model_root.mkdir(parents=True, exist_ok=True)
        log_stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        self._process_log_path = model_root / ('training-' + log_stamp + '.log')
        with self._process_log_path.open('a', encoding='utf-8') as training_log:
            training_log.write('training process started at ' + datetime.now(timezone.utc).isoformat() + '\n')
            training_log.flush()
            self._process = subprocess.Popen([sys.executable, '-u', '-m', 'service.model_training',
                str(root.resolve()), str(cfg.model_training_matches), str(cfg.model_training_cpu),
                str(cfg.model_training_memory_mb)], stdout=training_log, stderr=subprocess.STDOUT)
        self._process_started = time.monotonic()
        self._status = {**self._status, 'state': 'checking'}

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
                if (svc._thread is None or not svc._thread.is_alive() or
                        svc._live_thread is None or not svc._live_thread.is_alive() or
                        svc._persist_thread is None or not svc._persist_thread.is_alive()):
                    raise RuntimeError('模型后台意外退出')
        finally:
            svc.stop()


if __name__ == '__main__':
    main()
