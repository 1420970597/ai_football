"""Bounded atomic quote envelopes; never truncate a complete market snapshot."""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any

from .leyu_protocol import LeyuError

MAX_FIXTURE_QUOTES = 2000
MAX_MARKET_CHUNK = 500
MAX_BATCH_FIXTURES = 500  # Existing API FixtureBatch limit; byte/quote budgets still split first.
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
EMPTY_SNAPSHOT_BYTES = len(b'{"items":[]}')


def delivered_fixture_fingerprint(fixture: dict[str, Any]) -> str | None:
    """All delivered native facts, excluding receipt clocks only.

    Chunking is transport framing, not another market snapshot. Invalid or
    unsupported encodings conservatively disable the optional upload shortcut.
    """
    try:
        if not isinstance(fixture.get('id'), str) or not fixture['id']:
            return None
        value = {key: item for key, item in fixture.items() if key != 'received_at'}
        if 'market_chunks' in value:
            chunks = value.pop('market_chunks')
            if ('markets' in value or not isinstance(chunks, list)
                    or any(not isinstance(chunk, list) for chunk in chunks)):
                return None
            markets = [quote for chunk in chunks for quote in chunk]
            if (value.pop('market_chunk_count', None) != len(chunks)
                    or value.pop('market_quote_count', None) != len(markets)):
                return None
        else:
            markets = value.pop('markets', [])
        if (not isinstance(markets, list) or len(markets) > MAX_FIXTURE_QUOTES
                or any(not isinstance(quote, dict) for quote in markets)):
            return None
        value['markets'] = [{key: item for key, item in quote.items() if key != 'received_at'}
                            for quote in markets]
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                        allow_nan=False).encode()).hexdigest()
    except (TypeError, ValueError, OverflowError):
        return None


class DeliveredFixtureSnapshots:
    """Bounded process-local evidence of acknowledged complete HTTP payloads."""
    def __init__(self, capacity: int = 1024):
        if type(capacity) is not int or capacity < 1:
            raise ValueError('positive snapshot capacity required')
        self.capacity = capacity
        self._hashes: OrderedDict[str, str] = OrderedDict()

    def forget(self, fixtures: list[dict[str, Any]]) -> None:
        for fixture in fixtures:
            self._hashes.pop(fixture.get('id'), None)

    def remember(self, fixtures: list[dict[str, Any]]) -> None:
        for fixture in fixtures:
            self.forget([fixture])
            identity = delivered_fixture_fingerprint(fixture)
            if identity is not None:
                self._hashes[fixture['id']] = identity
                while len(self._hashes) > self.capacity:
                    self._hashes.popitem(last=False)

    def matches(self, fixture: dict[str, Any]) -> bool:
        identity = delivered_fixture_fingerprint(fixture)
        return identity is not None and self._hashes.get(fixture.get('id')) == identity


def snapshot_quote_count(fixture: dict[str, Any]) -> int:
    if "market_chunks" in fixture:
        return sum(len(chunk) for chunk in fixture["market_chunks"])
    return len(fixture.get("markets", []))


def fixture_envelope(fixture: dict[str, Any]) -> dict[str, Any]:
    markets = fixture.get("markets", [])
    if not isinstance(markets, list):
        raise LeyuError("invalid_fixture_markets")
    count = len(markets)
    if count > MAX_FIXTURE_QUOTES:
        raise LeyuError("fixture_quote_budget_exceeded")
    if count <= MAX_MARKET_CHUNK:
        return fixture
    return {**{key: value for key, value in fixture.items() if key != "markets"},
            "market_chunks": [markets[index:index + MAX_MARKET_CHUNK] for index in range(0, count, MAX_MARKET_CHUNK)],
            "market_chunk_count": (count + MAX_MARKET_CHUNK - 1) // MAX_MARKET_CHUNK,
            "market_quote_count": count}


def encode_snapshot(items: list[dict[str, Any]]) -> bytes:
    encoded = json.dumps({"items": items}, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise LeyuError("fixture_payload_budget_exceeded")
    return encoded


def prepare_fixture_batches(fixtures: list[dict[str, Any]], *, max_items: int = MAX_BATCH_FIXTURES,
                            quote_budget: int = MAX_FIXTURE_QUOTES) -> tuple[list[list[dict[str, Any]]], dict[str, Any]]:
    batches, batch, quotes = [], [], 0
    batch_bytes = EMPTY_SNAPSHOT_BYTES
    quarantined = []
    max_observed = 0
    for fixture in fixtures:
        count = len(fixture.get("markets", []))
        max_observed = max(max_observed, count)
        try:
            envelope = fixture_envelope(fixture)
            if count > quote_budget:
                raise LeyuError("fixture_quote_budget_exceeded")
            # Validate once. The compact JSON shell and inter-item commas
            # have fixed byte lengths, including for escaped/Unicode strings.
            # Re-encoding every growing prefix blocks the shared event loop.
            item_bytes = len(encode_snapshot([envelope])) - EMPTY_SNAPSHOT_BYTES
        except (LeyuError, ValueError) as exc:
            quarantined.append({"fixture_id": str(fixture.get("id") or ""),
                                "fixture_hash": hashlib.sha256(str(fixture.get("id") or "").encode()).hexdigest()[:10],
                                "quote_count": count,
                                "reason": exc.category if isinstance(exc, LeyuError) else "invalid_fixture_encoding"})
            continue
        if batch and (len(batch) >= max_items or quotes + count > quote_budget
                      or batch_bytes + 1 + item_bytes > MAX_SNAPSHOT_BYTES):
            batches.append(batch)
            batch = []
            quotes = 0
            batch_bytes = EMPTY_SNAPSHOT_BYTES
        batch_bytes += item_bytes + bool(batch)
        batch.append(envelope);quotes += count
    if batch:
        batches.append(batch)
    return batches, {"quarantined": quarantined, "max_observed_fixture_quotes": max_observed,
                     "quarantined_fixtures": len(quarantined), "batches": len(batches)}
