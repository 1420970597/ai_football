"""Optional bounded LLM review; never performs network I/O on the tick thread."""
from __future__ import annotations

import json
import math
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Mapping, Optional

from service.llm import LLMClient, LLMConfig, LLMError, extract_json
from service.runtime_settings import RuntimeConfig

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

    def configure(self, cfg: RuntimeConfig, version: int) -> None:
        client = (LLMClient(LLMConfig(base_url=cfg.llm_base_url, model=cfg.llm_model,
                                    api_key=cfg.llm_api_key or None, timeout_s=cfg.llm_timeout_s,
                                    max_retries=1, temperature=cfg.llm_temperature,
                                    max_tokens=cfg.llm_max_tokens)) if cfg.llm_enabled else None)
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

    def process_one(self) -> bool:
        with self._lock:
            if not self._queue:
                return False
            mid, row = self._queue.popitem(last=False)
            client, cfg, version = self._client, self._config, self._version
        if not client or row["config_version"] != version or time.time() * 1000 - row["published_at_ms"] > 30000:
            self.discarded += 1
            return True
        review = {"status": "ready", "config_version": version, "score": row["score"],
                  "input_at": row["computed_at"], "model": cfg.llm_model,
                  "published_at_ms": row["published_at_ms"]}
        try:
            prompt = json.dumps({k: row.get(k) for k in ("home", "away", "league", "score", "clock", "forecasts", "events")}, ensure_ascii=False)
            result = extract_json(client.complete(prompt, system=(
                "审核一份当时的足球滚球模型判断。只能使用输入的即时赛况，禁止编造终场或红牌。"
                "市场拟合概率没有独立预测优势。输出JSON: verdict(confirm/watch/reject), confidence(0到1), reason(简短中文)。"),
                max_tokens=cfg.llm_max_tokens))
            if not isinstance(result, dict) or result.get("verdict") not in ("confirm", "watch", "reject"):
                raise ValueError("无效审核结果")
            confidence = result.get("confidence")
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ValueError("无效置信度")
            review.update(verdict=result["verdict"], confidence=confidence, reason=str(result.get("reason") or "")[:160])
            self.completed += 1
        except (LLMError, ValueError, TypeError):
            review.update(status="error", reason="LLM审核失败或超时")
            self.errors += 1
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
                    or time.time() * 1000 - review["published_at_ms"] > 30000):
                return {"status": "expired", "input_at": review["input_at"]}
            return dict(review)

    def health(self) -> Dict[str, Any]:
        with self._lock:
            return {"enabled": self._config.llm_enabled, "pending": len(self._queue),
                    "completed": self.completed, "errors": self.errors, "discarded": self.discarded}
