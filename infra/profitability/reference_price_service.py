"""Evaluate a native offer against the recent verified reference archives."""

from datetime import datetime, timedelta, timezone
from decimal import InvalidOperation
import json
import os
from pathlib import Path
import re

from fastapi import FastAPI

from preflight_price_decision import evaluate
from reference_preflight import compare, instant

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
REFERENCE_AGE = 15
ARCHIVE_BYTES = 512 * 1024


def recent_records(directory, label, cutoff, time_key):
    paths = sorted(directory.glob(f"{label}-*.jsonl"), reverse=True)[:3]
    for path in paths:
        with path.open("rb") as archive:
            size = archive.seek(0, os.SEEK_END)
            if size > ARCHIVE_BYTES:
                archive.seek(size - ARCHIVE_BYTES)
                archive.readline()
            else:
                archive.seek(0)
            for line in archive.read(ARCHIVE_BYTES).splitlines(keepends=True):
                if not line.endswith(b"\n"):
                    break
                row = json.loads(line)
                if cutoff - timedelta(seconds=45) <= instant(row[time_key]) <= cutoff:
                    yield row


def price_offer(offer, root, now):
    cutoff = instant(offer["captured_at"])
    if not timedelta(0) <= now - cutoff <= timedelta(seconds=5):
        return {"status": "ineligible", "reasons": ["offer_cutoff_stale"]}
    manifest = json.loads((root / "reference-price.json").read_bytes())
    fixture = manifest["fixtures"].get(offer["fixture_id"])
    if not fixture:
        return {"status": "ineligible", "reasons": ["reference_contract_missing"]}
    label = fixture["label"]
    if not re.fullmatch(r"[a-z0-9-]{1,160}", label):
        raise ValueError("invalid reference archive label")
    approvals = fixture["approvals"]

    matches = compare(
        [offer], fixture["bindings"], recent_records(root / "books", label, cutoff, "captured_at"),
        REFERENCE_AGE, fixture["outcomes"], score_bindings=fixture.get("score_bindings"),
        score_records=recent_records(root / "scores", label, cutoff, "received_at"),
    )
    decision = evaluate(matches[0], approvals, REFERENCE_AGE)
    pricing = decision.get("pricing")
    if pricing:
        minimum = pricing["minimum_odds"]
        return {"status": decision["status"], "pricing": {
            "minimum_odds": str(minimum) if minimum is not None else None,
        }}
    return {"status": decision["status"], "reasons": decision.get("reasons", [])}


@app.post("/evaluate")
def evaluate_offer(offer: dict):
    root = Path(os.environ.get("REFERENCE_PRICE_ROOT", "/run/reference-data"))
    try:
        return price_offer(offer, root, datetime.now(timezone.utc))
    except (OSError, KeyError, ValueError, TypeError, InvalidOperation):
        return {"status": "ineligible", "reasons": ["reference_archive_unavailable"]}
