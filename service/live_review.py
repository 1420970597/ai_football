"""Optional bounded LLM review; never performs network I/O on the tick thread."""
from __future__ import annotations

import json
import copy
import math
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional

from service.llm import LLMClient, LLMConfig, LLMError, extract_json
from service.runtime_settings import RuntimeConfig
from store.history import HistoryJournal

MAX_QUEUE = 64
MAX_REVIEWS = 256


class LiveReview:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._queue: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._reviews: Dict[str, Dict[str, Any]] = {}
        self._submitted: Dict[str, float] = {}
        self._version = 1
        self._config = RuntimeConfig()
        self._client: Optional[LLMClient] = None
        self.completed = 0
        self.errors = 0
        self.discarded = 0
        self.on_experiment: Optional[Callable[[Mapping[str, Any], Mapping[str, Any]], int]] = None
        self.record_error = ""
        self.history: Optional[HistoryJournal] = None
        self._history_pending: list[Mapping[str, Any]] = []
        self._history_lock = threading.RLock()

    def bind_history(self, root: Optional[str]) -> None:
        with self._history_lock:
            path = Path(root) / 'llm-reviews.jsonl.gz' if root else None
            if path and (self.history is None or self.history.path != path):
                self.history = HistoryJournal(path)

    def flush_history(self) -> None:
        with self._history_lock:
            if self.history is None:
                return
            try:
                self.history.append(self._history_pending)
                self._history_pending.clear()
            except (OSError, ValueError, TypeError):
                pass  # Error is visible through history.health(); retry retains the batch.

    def configure(self, cfg: RuntimeConfig, version: int) -> None:
        client = None
        if cfg.llm_enabled:
            client = LLMClient(LLMConfig(
                base_url=cfg.llm_base_url, model=cfg.llm_model,
                api_key=cfg.llm_api_key or None, timeout_s=cfg.llm_timeout_s,
                max_retries=2, temperature=cfg.llm_temperature,
                max_tokens=cfg.llm_max_tokens,
                fallback_models=tuple(x.strip() for x in cfg.llm_fallback_models.split(',') if x.strip())))
        with self._lock:
            self._config, self._version, self._client = cfg, version, client
            self._queue.clear()
            self._reviews.clear()
            self._submitted.clear()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="live-llm-review", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(1)
        self.flush_history()

    def submit(self, row: Mapping[str, Any]) -> None:
        mid = str(row["match_id"])
        with self._lock:
            if not self._client or not row.get("forecasts") or row.get("config_version") != self._version:
                return
            now = time.monotonic()
            if now - self._submitted.get(mid, -math.inf) < self._config.llm_interval_s:
                return
            self._submitted[mid] = now
            self._queue[mid] = dict(row)
            if len(self._queue) > MAX_QUEUE:
                self._queue.popitem(last=False)
                self.discarded += 1
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(1)
            self._wake.clear()
            while not self._stop.is_set() and self.process_one():
                continue
            self.flush_history()

    def _record(self, row: Mapping[str, Any], review: Mapping[str, Any]) -> None:
        with self._history_lock:
            if self.history is not None:
                self._history_pending.append(copy.deepcopy({
                    'at': datetime.now(timezone.utc).isoformat(), 'schema_version': 1,
                    'decision': row, 'review': review}))
                self.flush_history()
        if self.on_experiment and self._config.llm_experiment_enabled:
            try:
                self.on_experiment(row,review)
                self.record_error = ''
            except (OSError,ValueError,TypeError):
                self.record_error = '实验记录写入失败'

    def process_one(self) -> bool:
        with self._lock:
            if not self._queue:
                return False
            mid, row = self._queue.popitem(last=False)
            client, cfg, version = self._client, self._config, self._version
        max_age_ms = cfg.llm_review_max_age_s * 1000
        if not client or row["config_version"] != version or time.time() * 1000 - row["published_at_ms"] > max_age_ms:
            self.discarded += 1
            self._record(row, {"status":"expired"})
            return True
        self._record(row, {"status":"pending"})
        started = time.monotonic()
        review = {"status": "ready", "config_version": version, "score": row["score"],
                  "input_at": row["computed_at"], "model": cfg.llm_model,
                  "published_at_ms": row["published_at_ms"]}
        try:
            inputs = {k: row.get(k) for k in ('home','away','league','score','clock','events')}
            inputs['forecasts'] = [{k:f.get(k) for k in ('market','line','outcome','odds','p_model','p_market','effective_ev','confidence')} for f in list(row.get('forecasts') or [])[:12]]
            prompt = json.dumps(inputs,ensure_ascii=False)
            result = extract_json(client.complete(prompt, system=(
                "审核一份当时的足球滚球模型判断。只能使用输入的即时赛况，禁止编造终场或红牌。"
                "市场拟合概率没有独立预测优势。输出JSON: verdict(confirm/watch/reject), confidence(0到1), reason(简短中文), "
                "assessments数组，逐盘口包含market,line,outcome,verdict,confidence。只评输入中的盘口，不要修改盘口或赔率。"),
                max_tokens=cfg.llm_max_tokens))
            if not isinstance(result, dict) or result.get("verdict") not in ("confirm", "watch", "reject"):
                raise ValueError("无效审核结果")
            confidence = result.get("confidence")
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ValueError("无效置信度")
            review.update(verdict=result["verdict"], confidence=confidence, reason=str(result.get("reason") or "")[:160])
            assessments = []
            for item in result.get('assessments',[]) if isinstance(result.get('assessments'),list) else []:
                if not isinstance(item,dict) or item.get('verdict') not in ('confirm','watch','reject'):
                    continue
                value = item.get('confidence')
                if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and 0<=value<=1:
                    assessments.append({k:item.get(k) for k in ('market','line','outcome','verdict','confidence')})
            review['assessments'] = assessments
            self.completed += 1
        except (LLMError, ValueError, TypeError) as exc:
            codes = re.findall(r'HTTP ([0-9]{3})',str(exc))
            code = 'upstream_http_'+codes[0] if codes else 'invalid_response' if isinstance(exc,(ValueError,TypeError)) else 'llm_unavailable'
            review.update(status='error',reason='LLM审核失败或超时',error_code=code)
            self.errors += 1
        review['latency_ms'] = round((time.monotonic()-started)*1000,3)
        if time.time()*1000-row['published_at_ms'] > cfg.llm_review_max_age_s * 1000 or version!=self._version:
            review['status']='expired'
        self._record(row,review)
        with self._lock:
            if version == self._version:
                self._reviews[mid] = review
                if len(self._reviews) > MAX_REVIEWS:
                    self._reviews.pop(next(iter(self._reviews)))
            else:
                self.discarded += 1
        return True

    def public(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        with self._lock:
            if not self._config.llm_enabled:
                return {"status": "disabled"}
            review = self._reviews.get(str(row["match_id"]))
            if not review:
                return {"status": "pending"}
            if (review["config_version"] != row["config_version"] or review["score"] != row["score"]
                    or time.time() * 1000 - review["published_at_ms"] > self._config.llm_review_max_age_s * 1000):
                return {"status": "expired", "input_at": review["input_at"]}
            return dict(review)

    def health(self) -> Dict[str, Any]:
        with self._lock:
            client_health = self._client.health() if self._client else {}
            return {"enabled": self._config.llm_enabled, "pending": len(self._queue),
                    "completed": self.completed, "errors": self.errors, "discarded": self.discarded,
                    "experiment_enabled": self._config.llm_experiment_enabled,
                    "history": {**(self.history.health() if self.history else {}),
                                'pending': len(self._history_pending)},
                    "record_error": self.record_error, "client": client_health}
