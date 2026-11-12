"""One absolute real-time signal budget; never turn a timeout into success."""
from datetime import datetime, timedelta, timezone

REALTIME_SIGNAL_DEADLINE_SECONDS = 20


def review_deadline_at(detected_at):
    if detected_at is None:
        return None
    return detected_at.astimezone(timezone.utc) + timedelta(seconds=REALTIME_SIGNAL_DEADLINE_SECONDS)


def remaining_review_seconds(detected_at, now=None):
    deadline = review_deadline_at(detected_at)
    if deadline is None:
        return None  # Legacy timestamps are unknown, not fresh detections.
    return max(0.0, (deadline - (now or datetime.now(timezone.utc))).total_seconds())
