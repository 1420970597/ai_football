"""Bounded internal quote refresh; shared upstream backoff contains no credentials."""

from __future__ import annotations

import asyncio
import math
import re
import time
from typing import Any, Awaitable, Callable

from .leyu import LeyuClient
from .leyu_protocol import LeyuError, RateLimited


class LeyuRequestBudget:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._blocked_until = 0.0
        self._precise_started_at = float("-inf")
        self._precise_by_mid: dict[str, float] = {}

    def cooldown_remaining(self) -> float:
        return max(0.0, self._blocked_until - self.clock())

    def defer_requests(self, seconds: float) -> None:
        if math.isfinite(seconds) and seconds > 0:
            self._blocked_until = max(self._blocked_until, self.clock() + min(900, seconds))

    def require_available(self) -> None:
        remaining = self.cooldown_remaining()
        if remaining:
            raise RateLimited("shared_upstream_cooldown", retry_after_seconds=math.ceil(remaining))

    def claim_precise_start(self, mid: str) -> None:
        """Enforce spacing at the provider call, after potentially slow config reads."""
        self.require_available()
        now = self.clock()
        if max(self._precise_started_at, self._precise_by_mid.get(mid, float("-inf"))) + 2 > now:
            raise LeyuError("refresh_throttled")
        self._precise_started_at = now
        self._precise_by_mid = {key: stamp for key, stamp in self._precise_by_mid.items() if now - stamp < 2}
        self._precise_by_mid[mid] = now


class LeyuRefreshController:
    def __init__(self, runtime, upload: Callable[..., Awaitable[int]], *,
                 deadline_seconds: float = 8, clock: Callable[[], float] = time.monotonic):
        self.runtime, self.upload = runtime, upload
        self.budget = getattr(runtime, "request_budget", None) or LeyuRequestBudget(clock=clock)
        self.clock = clock
        self.deadline_seconds = min(8, max(.01, deadline_seconds))
        self._last_start = float("-inf")
        self._last_by_mid: dict[str, float] = {}
        self._inflight: dict[str, dict[str, Any]] = {}

    def cooldown_remaining(self) -> float:
        return self.budget.cooldown_remaining()

    def defer_requests(self, seconds: float) -> None:
        self.budget.defer_requests(seconds)

    @staticmethod
    def _result(fixture_id: str, mid: str, reason: str, *, accepted: int = 0, retry: float = 0) -> dict[str, Any]:
        return {"fixture_id": fixture_id, "source_match_id": mid, "accepted": accepted == 1,
                "written": accepted == 1, "written_fixtures": accepted,
                "reason": reason, "retry_after_seconds": math.ceil(max(0, retry))}

    async def refresh(self, body: dict[str, Any]) -> dict[str, Any]:
        if set(body) != {"fixture_id", "source_match_id"}:
            raise ValueError("invalid refresh body")
        identity, requested_mid = body["fixture_id"], body["source_match_id"]
        if not isinstance(identity, str) or not 1 <= len(identity) <= 160:
            raise ValueError("invalid refresh identity")
        fixture = self.runtime.fixtures.get(identity)
        if not fixture:
            return self._result(identity, "", "fixture_not_loaded")
        mid = fixture.get("source_match_id")
        try:
            LeyuClient.validate_match_id(mid)
        except LeyuError:
            raise ValueError("invalid refresh identity") from None
        if not isinstance(requested_mid, str) or requested_mid not in {mid, fixture.get("source_id")}:
            raise ValueError("invalid refresh identity")
        if fixture.get("phase") not in {"scheduled", "live"}:
            return self._result(identity, requested_mid, "fixture_not_open")
        entry = self._inflight.get(identity)
        if entry is None:
            remaining = self.cooldown_remaining()
            if remaining:
                return self._result(identity, requested_mid, "upstream_cooldown", retry=remaining)
            if self._inflight:
                return self._result(identity, requested_mid, "refresh_busy")
            now = self.clock()
            remaining = max(self._last_start + 2 - now, self._last_by_mid.get(mid, float("-inf")) + 2 - now)
            if remaining > 0:
                return self._result(identity, requested_mid, "refresh_throttled", retry=remaining)
            self._last_start = now
            active = {str(item.get("source_match_id") or "") for item in self.runtime.fixtures.values()}
            self._last_by_mid = {key: stamp for key, stamp in self._last_by_mid.items() if key in active}
            self._last_by_mid[mid] = now
            entry = {"task": asyncio.create_task(self._refresh(identity, mid)), "waiters": 0}
            self._inflight[identity] = entry
        entry["waiters"] += 1
        try:
            result = await asyncio.shield(entry["task"])
            return {**result, "source_match_id": requested_mid}
        finally:
            entry["waiters"] -= 1
            if not entry["waiters"]:
                self._inflight.pop(identity, None)
                if not entry["task"].done():
                    entry["task"].cancel()
                    await asyncio.gather(entry["task"], return_exceptions=True)

    async def _refresh(self, identity: str, mid: str) -> dict[str, Any]:
        stage = "source"
        try:
            async with asyncio.timeout(self.deadline_seconds):
                fixture = await self.runtime.refresh_match(mid)
                if fixture.get("id") != identity or str(fixture.get("source_match_id") or "") != mid:
                    raise LeyuError("fixture_identity_mismatch")
                observation = fixture.pop("_current_observation", None)
                stage = "ingestion"
                accepted = await self.upload([fixture])
                if type(accepted) is not int or accepted not in {0, 1}:
                    raise LeyuError("invalid_ingestion_receipt")
                if accepted == 0:
                    # Parent and quote clocks are independent. A committed batch
                    # may update quotes while rejecting its stale parent; do not
                    # invent a quote row count from the parent receipt.
                    return {**self._result(identity, mid, "parent_unchanged_recheck_quotes"),
                            "snapshot_committed": True, "quote_writes_verified": False,
                            "current_observation": observation}
                return {**self._result(identity, mid, "written", accepted=1),
                        "snapshot_committed": True, "quote_writes_verified": False,
                        "current_observation": observation}
        except RateLimited as exc:
            self.defer_requests(max(60, exc.retry_after_seconds or 0))
            return self._result(identity, mid, "upstream_rate_limited", retry=self.cooldown_remaining())
        except TimeoutError:
            report = getattr(self.runtime, "last_refresh_report", {})
            if stage == "source" and report.get("stage") == "lock_wait":
                return self._result(identity, mid, "refresh_busy")
            if stage == "source":
                self.defer_requests(2)
            return self._result(identity, mid, "refresh_timeout")
        except LeyuError as exc:
            if stage == "source" and exc.category not in {"refresh_busy", "refresh_throttled", "fixture_not_loaded", "fixture_not_open"}:
                self.defer_requests(2)
            reason = exc.category if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", exc.category) else "refresh_failed"
            return self._result(identity, mid, reason)
        except Exception:
            if stage == "source":
                self.defer_requests(2)
            return self._result(identity, mid, "ingestion_failed" if stage == "ingestion" else "refresh_failed")

    async def close(self) -> None:
        tasks = [entry["task"] for entry in self._inflight.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
