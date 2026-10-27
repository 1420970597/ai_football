"""Bounded atomic quote envelopes; never truncate a complete market snapshot."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .leyu_protocol import LeyuError

MAX_FIXTURE_QUOTES = 2000
MAX_MARKET_CHUNK = 500
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024


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


def prepare_fixture_batches(fixtures: list[dict[str, Any]], *, max_items: int = 250,
                            quote_budget: int = MAX_FIXTURE_QUOTES) -> tuple[list[list[dict[str, Any]]], dict[str, Any]]:
    batches, batch, quotes = [], [], 0
    quarantined = []
    max_observed = 0
    for fixture in fixtures:
        count = len(fixture.get("markets", []))
        max_observed = max(max_observed, count)
        try:
            envelope = fixture_envelope(fixture)
            if count > quote_budget:
                raise LeyuError("fixture_quote_budget_exceeded")
            encode_snapshot([envelope])
        except (LeyuError, ValueError) as exc:
            quarantined.append({"fixture_id": str(fixture.get("id") or ""),
                                "fixture_hash": hashlib.sha256(str(fixture.get("id") or "").encode()).hexdigest()[:10],
                                "quote_count": count,
                                "reason": exc.category if isinstance(exc, LeyuError) else "invalid_fixture_encoding"})
            continue
        if batch and (len(batch) >= max_items or quotes + count > quote_budget):
            batches.append(batch);batch=[];quotes=0
        if batch:
            try:
                encode_snapshot([*batch, envelope])
            except LeyuError:
                batches.append(batch);batch=[];quotes=0
        batch.append(envelope);quotes += count
    if batch:
        batches.append(batch)
    return batches, {"quarantined": quarantined, "max_observed_fixture_quotes": max_observed,
                     "quarantined_fixtures": len(quarantined), "batches": len(batches)}
