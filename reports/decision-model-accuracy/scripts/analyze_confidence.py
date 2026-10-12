#!/usr/bin/env python3
"""Group settled ensemble recommendations by decision-time confidence.

The workbench's ``composite_confidence`` is derived from the persisted ensemble
confidence and settled evidence for the same quote. Historical rows do not
store that derived value, so this script replays the evidence as-of each
decision timestamp. Later settlements never influence an earlier row.
"""
from __future__ import annotations

import csv
import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB = ROOT / "output" / "ledger" / "ledger.sqlite3"
DEFAULT_OUTPUT = (ROOT / "reports" / "decision-model-accuracy" / "data"
                  / "composite_confidence_accuracy.csv")
CONFIDENCE_STEP = 0.1
CONFIDENCE_BINS = tuple((round(i * CONFIDENCE_STEP, 1), round((i + 1) * CONFIDENCE_STEP, 1))
                       for i in range(10))
GRADED_STATUSES = {"won", "lost", "half_won", "half_lost", "push"}
FILTER_SQL = """competition_type='real'
    AND algorithm='economic_ensemble'
    AND trigger='live_recommendation'
    AND research_only=0
    AND is_pick=1"""


@dataclass
class Bucket:
    records: int = 0
    decision_occurrences: int = 0
    settled_records: int = 0
    settled_occurrences: int = 0
    pending_records: int = 0
    pending_occurrences: int = 0
    void_records: int = 0
    void_occurrences: int = 0
    full_won: int = 0
    half_won: int = 0
    full_lost: int = 0
    half_lost: int = 0
    push: int = 0
    weighted_wins: float = 0.0
    weighted_losses: float = 0.0
    profit_units: float = 0.0
    confidence_sum: float = 0.0
    confidence_min: float = math.inf
    confidence_max: float = -math.inf
    base_confidence_sum: float = 0.0
    base_confidence_min: float = math.inf
    base_confidence_max: float = -math.inf
    matches: set[str] | None = None
    first_at: str = ""
    last_at: str = ""

    def __post_init__(self) -> None:
        self.matches = set()

    def add(self, row: sqlite3.Row, confidence: float) -> None:
        base_confidence = float(row["confidence"])
        occurrences = max(1, int(row["decision_count"] or 1))
        status = str(row["status"] or "")
        self.records += 1
        self.decision_occurrences += occurrences
        self.confidence_sum += confidence
        self.confidence_min = min(self.confidence_min, confidence)
        self.confidence_max = max(self.confidence_max, confidence)
        self.base_confidence_sum += base_confidence
        self.base_confidence_min = min(self.base_confidence_min, base_confidence)
        self.base_confidence_max = max(self.base_confidence_max, base_confidence)
        self.matches.add(str(row["match_id"] or ""))
        at = str(row["at"] or "")
        self.first_at = min(self.first_at, at) if self.first_at else at
        self.last_at = max(self.last_at, at) if self.last_at else at
        if status == "pending":
            self.pending_records += 1
            self.pending_occurrences += occurrences
        elif status == "void":
            self.void_records += 1
            self.void_occurrences += occurrences
        elif status in GRADED_STATUSES:
            self.settled_records += 1
            self.settled_occurrences += occurrences
            self.profit_units += float(row["pnl"] or 0.0) * occurrences
            if status == "won":
                self.full_won += occurrences
                self.weighted_wins += occurrences
            elif status == "half_won":
                self.half_won += occurrences
                self.weighted_wins += occurrences * 0.5
            elif status == "lost":
                self.full_lost += occurrences
                self.weighted_losses += occurrences
            elif status == "half_lost":
                self.half_lost += occurrences
                self.weighted_losses += occurrences * 0.5
            elif status == "push":
                self.push += occurrences

    def as_row(self, scope: str, model_version: str, low: float, high: float) -> dict[str, object]:
        denominator = self.weighted_wins + self.weighted_losses
        accuracy = self.weighted_wins / denominator if denominator else None
        graded = self.settled_occurrences
        roi = self.profit_units / graded if graded else None
        ci_low, ci_high = wilson_interval(self.weighted_wins, denominator)
        return {
            "scope": scope,
            "model_version": model_version,
            "confidence_band": f"{low:.0%}-{high:.0%}",
            "lower_bound": low,
            "upper_bound": high,
            "records": self.records,
            "matches": len(self.matches),
            "decision_occurrences": self.decision_occurrences,
            "settled_records": self.settled_records,
            "settled_occurrences": self.settled_occurrences,
            "pending_records": self.pending_records,
            "pending_occurrences": self.pending_occurrences,
            "void_records": self.void_records,
            "void_occurrences": self.void_occurrences,
            "full_won_occurrences": self.full_won,
            "half_won_occurrences": self.half_won,
            "full_lost_occurrences": self.full_lost,
            "half_lost_occurrences": self.half_lost,
            "push_occurrences": self.push,
            "weighted_wins": round(self.weighted_wins, 6),
            "weighted_losses": round(self.weighted_losses, 6),
            "accuracy": round(accuracy, 6) if accuracy is not None else "",
            "accuracy_ci95_low": round(ci_low, 6) if ci_low is not None else "",
            "accuracy_ci95_high": round(ci_high, 6) if ci_high is not None else "",
            "profit_units": round(self.profit_units, 6),
            "settled_unit_roi": round(roi, 6) if roi is not None else "",
            "avg_composite_confidence": (round(self.confidence_sum / self.records, 6)
                                          if self.records else ""),
            "min_composite_confidence": (round(self.confidence_min, 6)
                                          if self.records else ""),
            "max_composite_confidence": (round(self.confidence_max, 6)
                                          if self.records else ""),
            "avg_persisted_confidence": (round(self.base_confidence_sum / self.records, 6)
                                          if self.records else ""),
            "min_persisted_confidence": (round(self.base_confidence_min, 6)
                                          if self.records else ""),
            "max_persisted_confidence": (round(self.base_confidence_max, 6)
                                          if self.records else ""),
            "first_decision_at": self.first_at,
            "last_decision_at": self.last_at,
            "accuracy_definition": ("weighted_wins/(weighted_wins+weighted_losses); "
                                    "half win/loss=0.5; push excluded"),
            "confidence_definition": ("as-of replay of runtime composite: persisted base "
                                      "confidence plus prior same-quote settled evidence; "
                                      "no future labels"),
        }


def wilson_interval(successes: float, trials: float,
                    z: float = 1.96) -> tuple[float | None, float | None]:
    """Wilson interval using weighted win/loss observations as pseudo-counts."""
    if trials <= 0:
        return None, None
    p = successes / trials
    denominator = 1.0 + z * z / trials
    centre = (p + z * z / (2.0 * trials)) / denominator
    margin = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * trials)) / trials) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def confidence_bin(value: float) -> tuple[float, float]:
    bounded = min(1.0, max(0.0, value))
    if bounded >= 1.0:
        return CONFIDENCE_BINS[-1]
    index = min(9, int(bounded / CONFIDENCE_STEP))
    return CONFIDENCE_BINS[index]


def read_rows(db_path: Path) -> list[sqlite3.Row]:
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        return connection.execute(
            "SELECT payload, model_version, confidence, decision_count, match_id, at, status, "
            "settled_at, pnl "
            "FROM decisions WHERE " + FILTER_SQL + " ORDER BY at, identity"
        ).fetchall()
    finally:
        connection.close()


def _evidence_key(row: sqlite3.Row) -> tuple[str, str, str, str, str, str]:
    payload = json.loads(row["payload"])
    return (str(row["model_version"] or ""),
            str(payload.get("decision_model_version") or ""),
            str(payload.get("algorithm") or "economic_ensemble"),
            str(payload.get("market") or ""), str(payload.get("line") or ""),
            str(payload.get("outcome") or ""))


def as_of_composite_confidence(rows: list[sqlite3.Row]) -> list[float]:
    """Replay the workbench composite confidence at each decision timestamp."""
    events: list[tuple[str, int, tuple[str, str, str, str, str, str], str, int]] = []
    for index, row in enumerate(rows):
        settled_at = str(row["settled_at"] or "")
        at = str(row["at"] or "")
        status = str(row["status"] or "")
        if settled_at and at < settled_at and status in {"won", "lost", "half_won", "half_lost"}:
            events.append((settled_at, index, _evidence_key(row), status,
                           max(1, int(row["decision_count"] or 1))))
    events.sort(key=lambda item: (item[0], item[1]))
    evidence: dict[tuple[str, str, str, str, str, str], list[float]] = {}
    result: list[float] = []
    event_index = 0
    for row in rows:
        decision_at = str(row["at"] or "")
        while event_index < len(events) and events[event_index][0] <= decision_at:
            _, _, key, status, occurrences = events[event_index]
            state = evidence.setdefault(key, [0.0, 0.0])
            if status == "won":
                state[0] += occurrences
            elif status == "half_won":
                state[0] += occurrences * 0.5
            elif status == "lost":
                state[1] += occurrences
            else:
                state[1] += occurrences * 0.5
            event_index += 1
        hits, misses = evidence.get(_evidence_key(row), (0.0, 0.0))
        observed = (hits + 1.0) / (hits + misses + 2.0)
        base = float(row["confidence"] or 0.0)
        result.append(round(base * (0.5 + 0.5 * observed), 6))
    return result


def generate(db_path: Path = DEFAULT_DB,
             output_path: Path = DEFAULT_OUTPUT) -> list[dict[str, object]]:
    rows = read_rows(db_path)
    scopes = {"all"} | {str(row["model_version"] or "") for row in rows}
    buckets: dict[tuple[str, str, tuple[float, float]], Bucket] = defaultdict(Bucket)
    composite_confidences = as_of_composite_confidence(rows)
    for row, confidence in zip(rows, composite_confidences):
        model = str(row["model_version"] or "")
        band = confidence_bin(confidence)
        buckets[("all", "all", band)].add(row, confidence)
        buckets[("model", model, band)].add(row, confidence)

    result: list[dict[str, object]] = []
    for scope, model_version in (("all", "all"),) + tuple(
            ("model", model) for model in sorted(scopes - {"all"})):
        for low, high in CONFIDENCE_BINS:
            result.append(buckets[(scope, model_version, (low, high))].as_row(
                scope, model_version, low, high))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fields = tuple(result[0].keys())
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(result)
    return result


def main() -> None:
    rows = generate()
    populated = [row for row in rows if int(row["records"]) > 0]
    print(f"Generated {len(rows)} rows ({len(populated)} populated) from {DEFAULT_DB}")


if __name__ == "__main__":
    main()
