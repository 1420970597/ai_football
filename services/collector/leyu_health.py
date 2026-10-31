"""Bounded collector diagnostics; no notification transport or upstream assumptions."""

from __future__ import annotations

from datetime import datetime, timezone
import math


class ThresholdAlert:
    """Hysteresis and consecutive samples suppress repeated failure transitions."""

    def __init__(self, trigger: float, recover: float, *, raise_after: int = 2, recover_after: int = 3):
        if recover >= trigger or min(raise_after, recover_after) < 1:
            raise ValueError("invalid alert threshold")
        self.trigger, self.recover = trigger, recover
        self.raise_after, self.recover_after = raise_after, recover_after
        self.active = False
        self.state = "unknown"
        self.bad_samples = self.good_samples = 0
        self.transitions = self.recoveries = 0
        self.changed_at: str | None = None
        self.recovered_at: str | None = None

    def observe(self, value: float | None, *, now: datetime | None = None) -> bool:
        if value is None or not math.isfinite(value):
            # Missing samples neither declare health nor heal an active alert.
            return False
        changed = False
        if value >= self.trigger:
            self.bad_samples += 1
            self.good_samples = 0
            if not self.active and self.bad_samples >= self.raise_after:
                self.active, changed = True, True
            self.state = "active" if self.active else "pending"
        elif value <= self.recover:
            self.good_samples += 1
            self.bad_samples = 0
            if self.active and self.good_samples >= self.recover_after:
                self.active, changed = False, True
                self.recoveries += 1
                self.recovered_at = (now or datetime.now(timezone.utc)).isoformat()
            self.state = "recovering" if self.active else "clear"
        else:
            self.bad_samples = self.good_samples = 0
            self.state = "active" if self.active else "clear"
        if changed:
            self.transitions += 1
            self.changed_at = (now or datetime.now(timezone.utc)).isoformat()
        return changed

    def fields(self, prefix: str) -> dict:
        return {f"{prefix}_state": self.state, f"{prefix}_transitions": self.transitions,
                f"{prefix}_recoveries": self.recoveries, f"{prefix}_changed_at": self.changed_at,
                f"{prefix}_recovered_at": self.recovered_at}
