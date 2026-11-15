"""Detection latency is observed, not a business eligibility deadline.

The user removed the 20-second hard requirement on 2026-11-15. Keep the
legacy helpers for historical receipts without granting fresh timestamps.
Worker/provider/database timeouts and active lease/quote guards remain.
"""
from datetime import datetime, timedelta, timezone

REALTIME_SIGNAL_DEADLINE_SECONDS = None


def review_deadline_at(detected_at):
    if detected_at is None or REALTIME_SIGNAL_DEADLINE_SECONDS is None:
        return None
    return detected_at.astimezone(timezone.utc) + timedelta(seconds=REALTIME_SIGNAL_DEADLINE_SECONDS)


def remaining_review_seconds(detected_at, now=None):
    deadline = review_deadline_at(detected_at)
    if deadline is None:
        return None  # Legacy timestamps are unknown, not fresh detections.
    return max(0.0, (deadline - (now or datetime.now(timezone.utc))).total_seconds())
