"""Single background publisher; browser reads never build a board or query history."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Mapping


# These are decision/training diagnostics, not fields rendered by a buy card.
# Keep the other pick values verbatim, especially evidence counts and live odds.
_PICK_DIAGNOSTICS = frozenset({
    'feature_context', 'weights', 'members', 'training_history', 'order_detail',
    'model_application', 'blend_weights', 'decision_evidence',
    'ts_ms', 'quote_received_at', 'quote_received_at_ms', 'input_cutoff_ms',
    'quote_time_ms', 'evidence_at', 'probability_sum',
    'q_fill', 'risk_penalty', 'devig_spread_pp', 'win_stake', 'loss_stake',
    'push_stake', 'tail', 'disagreement',
})
_CLOSED_DISPLAY_FIELDS = frozenset({
    'at', 'match_id', 'decision_id', 'algorithm', 'competition_type', 'league',
    'home', 'away', 'market', 'line', 'outcome', 'label', 'market_label', 'odds',
    'status', 'pnl', 'correct',
})
_MAX_BROWSER_INTEGER = 2 ** 53 - 1


def compact_recommendations(payload: Mapping[str, Any], limit: int = 100,
                            offset: int = 0) -> dict[str, Any]:
    """Project a published recommendation snapshot without changing its data.

    Pagination covers the builder's recent closed window, not the whole ledger.
    Its independently computed summary is retained, including when the summary
    covers more decisions than that window. No open selection is paginated.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError('closed_limit must be an integer between 1 and 100')
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError('closed_offset must be a nonnegative integer')
    closed = payload.get('closed') or []
    total = len(closed)
    # A refresh may shrink the visible window while the browser is on its end.
    # Clamp only out-of-range offsets, preserving arbitrary valid API offsets.
    if not total:
        offset = 0
    elif offset >= total:
        offset = ((total - 1) // limit) * limit
    page = [{key: value for key, value in item.items() if key in _CLOSED_DISPLAY_FIELDS}
            for item in closed[offset:offset + limit]]
    open_items = []
    for item in payload.get('open') or []:
        public = dict(item)
        pick = item.get('pick')
        if isinstance(pick, Mapping):
            public['pick'] = {key: value for key, value in pick.items()
                              if key not in _PICK_DIAGNOSTICS}
        open_items.append(public)
    summary_total = (payload.get('summary') or {}).get('closed', total)
    if (isinstance(summary_total, bool) or not isinstance(summary_total, int)
            or not 0 <= summary_total <= _MAX_BROWSER_INTEGER):
        summary_total = total
    return {**payload, 'open': open_items, 'closed': page, 'closed_page': {
        'offset': offset, 'limit': limit, 'returned': len(page),
        'total_available': total,
        'next_offset': offset + limit if offset + limit < total else None,
        'prev_offset': max(0, offset - limit) if offset else None,
        'summary_total': summary_total, 'window_truncated': summary_total > total,
        'scope': 'recent_recommendations',
    }}


class ResponseProjection:
    def __init__(self, build: Callable[[str, str], dict[str, Any]], interval: float = 1.0) -> None:
        self.build = build
        self.interval = interval
        self._lock = threading.Lock()
        self._wanted: set[tuple[str, str]] = set()
        self._values: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._errors: dict[tuple[str, str], str] = {}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='api-projection', daemon=True)
        self._thread.start()

    def read(self, kind: str, name: str, empty: dict[str, Any]) -> dict[str, Any]:
        key = (kind, name)
        with self._lock:
            first = key not in self._wanted
            self._wanted.add(key)
            value = self._values.get(key)
            error = self._errors.get(key, '')
        if first:
            self._wake.set()
        age = max(0.0, time.monotonic()-value[0]) if value else None
        # Published containers are immutable. Only add per-request metadata;
        # deep-copying thousands of rows on every browser poll defeats isolation.
        return {**(value[1] if value else empty), 'projection': {
            'loading': value is None, 'age_s': round(age, 3) if age is not None else None,
            'stale': bool(error) or age is None or age > self.interval*3,
            'error': error}}

    def refresh(self) -> None:
        with self._lock:
            keys = list(self._wanted)
        for kind, name in sorted(keys):
            if self._stop.is_set():
                return
            try:
                payload = self.build(kind, name)
                with self._lock:
                    self._values[(kind, name)] = (time.monotonic(), payload)
                    self._errors.pop((kind, name), None)
            except Exception:
                # Keep the last good snapshot and expose its age. Provider
                # response text and credentials must never enter this API.
                with self._lock:
                    self._errors[(kind, name)] = '后台数据更新失败，保留上次结果'

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._wake.wait(self.interval)
            self._wake.clear()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(5)
