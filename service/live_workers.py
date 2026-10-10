"""Persistent processes for live calculation and replay encoding; no venue calls."""
from __future__ import annotations

import multiprocessing
import pickle
import threading
import time
import zlib
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping


class LiveWorkerError(ValueError):
    """The isolated worker failed; its result must not be published."""


@dataclass
class FrozenReplay:
    """Immutable calculation input/output plus small final publication metadata."""
    payload: bytes
    metadata: dict[str, Any] = field(default_factory=dict)
    histories: bytes | None = None

    def materialize(self) -> dict[str, Any]:
        return {**pickle.loads(self.payload), **self.metadata}

    def price_history(self) -> dict[str, Any] | None:
        return pickle.loads(self.histories) if self.histories is not None else None


def freeze_replay(result: Mapping[str, Any], snapshot: Mapping[str, Any],
                  config: Any, anchor: Mapping[str, Any], histories: Mapping | None = None) -> FrozenReplay:
    frozen_config = asdict(config)
    frozen_config.pop('llm_api_key', None)
    record = {**result, 'at': result['computed_at'], 'schema_version': 2,
              'config': frozen_config, 'anchor': dict(anchor),
              'input_state': {k: snapshot.get(k) for k in
                  ('info', 'status', 'score_age_s', 'status_age_s', 'half_score',
                   'finished', 'suspended', 'suspended_ids', 'received_at_ms', 'captured_at_ms')},
              'input_events': list(snapshot.get('events') or [])}
    frozen_history = (pickle.dumps({'|'.join(key[1:]): values for key, values in histories.items()}, protocol=5)
                      if histories is not None else None)
    # Rolling detail history is an opaque UI cache. Full original prices already
    # live in TrendStore; do not re-archive forty RAW points on every decision.
    return FrozenReplay(pickle.dumps(record, protocol=5), histories=frozen_history)


def _calculate_loop(pipe: Any) -> None:
    from service.live_expert import LiveExpertService
    service = LiveExpertService()
    recent: OrderedDict[str, None] = OrderedDict()
    pipe.send(('ready', None))
    try:
        while True:
            job = pipe.recv()
            if job is None:
                return
            snapshot, config, version, performance, replay, compact = job
            try:
                if version != service.config_version or config != service.config:
                    service.configure(config, version)
                service._performance = performance
                result = service._calculate(snapshot)
                mid = str(snapshot['match_id'])
                histories = {key: tuple(values) for key, values in service._series.items() if key[0] == mid}
                anchor = dict(service._anchors.get(mid, {}))
                started = time.perf_counter()
                record = freeze_replay(result, snapshot, config, anchor,
                                       histories if compact else None) if replay else None
                if compact:
                    # RAW options are not consumed by any current algorithm.
                    # Their complete prices/history stay in the opaque replay;
                    # detail requests can decode them without taxing every tick.
                    result['markets'] = [m for m in result['markets'] if m.get('known')]
                    result.pop('candidates', None)
                    result['evaluations'] = [{k:v for k,v in evaluation.items() if k!='candidates'}
                                              for evaluation in result['evaluations']]
                    result['ensemble'] = {k:v for k,v in result['ensemble'].items() if k!='candidates'}
                    histories = {key: values for key, values in histories.items()
                                 if not key[1].startswith('RAW_')}
                result['freeze_ms'] = (time.perf_counter()-started)*1000
                pipe.send(('ok', (result, anchor, histories, record)))
                recent.pop(mid, None)
                recent[mid] = None
                expired = [mid] if result.get('finished') else []
                if len(recent) > 128:
                    expired.append(next(iter(recent)))
                for old in expired:
                    recent.pop(old, None)
                    service._anchors.pop(old, None)
                    for key in [key for key in service._series if key[0] == old]:
                        service._series.pop(key, None)
                        service._training_series.pop(key, None)
                        service._training_prefixes.pop(key, None)
            except (ValueError, TypeError, ArithmeticError, AttributeError, KeyError) as exc:
                pipe.send(('error', type(exc).__name__))
    except (EOFError, BrokenPipeError):
        return
    finally:
        pipe.close()


def _history_loop(pipe: Any, path: str) -> None:
    from store.history import HistoryJournal
    journal = HistoryJournal(Path(path))
    pipe.send(('ready', None))
    try:
        while True:
            rows = pipe.recv()
            if rows is None:
                return
            try:
                pipe.send(('ok', journal.append([
                    row.materialize() if isinstance(row, FrozenReplay) else row for row in rows])))
            except (OSError, ValueError, TypeError) as exc:
                pipe.send(('error', type(exc).__name__))
    except (EOFError, BrokenPipeError):
        return
    finally:
        pipe.close()


class _ProcessSlot:
    def __init__(self, target: Any, name: str, args: tuple = ()) -> None:
        self.target, self.name, self.args = target, name, args
        self.lock = threading.Lock()
        self.pipe: Any = None
        self.process: Any = None
        self.failures = 0
        self.pending = False

    def start(self, timeout: float = 10.0) -> None:
        if self.process is not None and self.process.is_alive():
            return
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe()
        process = context.Process(target=self.target, args=(child, *self.args), name=self.name, daemon=True)
        process.start()
        child.close()
        self.pipe, self.process = parent, process
        if not parent.poll(max(0.001, timeout)) or parent.recv()[0] != 'ready':
            self.close()
            raise LiveWorkerError('独立工作进程未就绪')

    def call(self, payload: Any, timeout: float, *, durable: bool = False) -> Any:
        started = time.monotonic()
        if not self.lock.acquire(timeout=max(0.001, timeout)):
            raise LiveWorkerError('独立计算进程排队超时')
        try:
            if self.process is None or not self.process.is_alive():
                # Recovery happens here, never by executing CPU work in the API.
                self.close()
                self.start(timeout=max(0.001, timeout-(time.monotonic()-started)))
            if not self.pending:
                self.pipe.send(payload)
                self.pending = True
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0 or not self.pipe.poll(remaining):
                raise LiveWorkerError('独立工作进程响应超时')
            status, result = self.pipe.recv()
            self.pending = False
            if not durable and time.monotonic() - started > timeout:
                raise LiveWorkerError('独立工作进程超过执行预算')
            if status != 'ok':
                raise LiveWorkerError('独立工作进程计算或持久化失败')
            return result
        except (OSError, EOFError, LiveWorkerError) as exc:
            self.failures += 1
            # Never terminate an active durable append on a response timeout:
            # doing so could leave a partial gzip member. Drain its original
            # acknowledgement on the next flush, without sending it twice.
            if not durable or not self.process or not self.process.is_alive() or not self.pending:
                self.close()
            raise LiveWorkerError(str(exc)) from exc
        finally:
            self.lock.release()

    def close(self) -> None:
        process, pipe = self.process, self.pipe
        self.process = self.pipe = None
        self.pending = False
        if process is not None:
            if process.is_alive():
                process.terminate()
            process.join(1)
            if process.is_alive():
                process.kill()
                process.join(1)
            process.close()
        if pipe is not None:
            pipe.close()


class LiveCalculationPool:
    """Stable per-match ownership keeps anchors and temporal prefixes in order."""

    def __init__(self, processes: int) -> None:
        self.slots = [_ProcessSlot(_calculate_loop, 'live-calculate-%d' % index)
                      for index in range(max(1, min(4, processes)))]

    def start(self) -> None:
        for slot in self.slots:
            slot.start()

    def shard(self, mid: str) -> int:
        return zlib.crc32(str(mid).encode('utf-8')) % len(self.slots)

    def calculate(self, snapshot: Mapping[str, Any], config: Any, version: int,
                  performance: Mapping[str, Any], timeout: float = 1.0, *, replay: bool = False,
                  compact: bool = False) -> tuple:
        if compact and not replay:
            raise ValueError('紧凑计算返回必须保留完整冻结记录')
        # Keys are never needed by the algorithms and must not cross this IPC.
        result = self.slots[self.shard(str(snapshot['match_id']))].call(
            (dict(snapshot), replace(config, llm_api_key=''), version, dict(performance), replay, compact), timeout)
        return result if replay else result[:3]

    def close(self) -> None:
        for slot in self.slots:
            with slot.lock:
                slot.close()

    def health(self) -> dict[str, Any]:
        return {'mode': 'process', 'processes': len(self.slots),
                'ready': sum(bool(s.process and s.process.is_alive()) for s in self.slots),
                'failures': sum(s.failures for s in self.slots)}


class ReplayProcessWriter:
    """JSON encoding/compression is isolated from socket/API Python execution."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.slot = _ProcessSlot(_history_loop, 'live-history-writer', (str(path),))
        self.slot.start()

    def append(self, rows: Any) -> int:
        return int(self.slot.call(rows, 10.0, durable=True))

    def close(self) -> None:
        with self.slot.lock:
            self.slot.close()
