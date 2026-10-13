#!/usr/bin/env python3
"""Collect a reproducible, two-track literature catalogue from OpenAlex.

The catalogue is metadata research, not a claim that every abstract was read
in full. Ranking and category rules are explicit so a later run can audit or
replace individual records without changing the report's evidence boundary.
"""
from __future__ import annotations

import csv
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
API = "https://api.openalex.org/works"
PER_PAGE = 100
MINIMUM = 50

ALGORITHM_QUERIES = (
    "sports betting algorithm wagering decision",
    "football betting strategy value betting odds",
    "Kelly criterion portfolio betting risk",
    "risk constrained Kelly gambling",
    "proper scoring rules probabilistic forecast",
    "probability calibration reliability forecast",
    "forecast combination ensemble weighting",
    "time series cross validation concept drift leakage",
    "sports bookmaker odds market efficiency",
    "betting exchange expected value staking",
    "online learning adaptive ensemble prediction",
    "decision theory uncertainty probability forecast",
)

MODEL_QUERIES = (
    "football match prediction poisson model",
    "soccer outcome prediction machine learning",
    "football Dixon Coles model",
    "dynamic poisson football goals",
    "in play soccer goal prediction model",
    "football Bayesian prediction model",
    "soccer neural network match prediction",
    "football random forest match outcome",
    "football gradient boosting prediction",
    "expected goals football model",
    "football hazard survival goal model",
    "bivariate poisson soccer scores",
)

ALGORITHM_TERMS = {
    "bet", "wager", "kelly", "odds", "staking", "portfolio", "scoring",
    "calibrat", "reliab", "forecast", "ensemble", "combination", "decision",
    "market efficiency", "bookmaker", "value", "risk", "drift", "validation",
    "leakage", "online learning", "probabilistic", "prediction interval",
}
MODEL_TERMS = {
    "football", "soccer", "goal", "match", "poisson", "dixon", "bayesian",
    "neural", "machine learning", "random forest", "gradient", "boosting",
    "hazard", "survival", "expected goals", "xg", "in-play", "dynamic",
    "bivariate", "deep learning", "logistic", "svm", "markov",
}
FOOTBALL_TERMS = {"football", "soccer", "goal", "match", "sport"}


def abstract_text(index: dict[str, list[int]] | None) -> str:
    if not index:
        return ""
    words = [""] * (max((n for positions in index.values() for n in positions), default=-1) + 1)
    for word, positions in index.items():
        for position in positions:
            if 0 <= position < len(words):
                words[position] = word
    return " ".join(words)


def fetch(query: str) -> list[dict[str, Any]]:
    params = urlencode({
        "search": query,
        "per-page": PER_PAGE,
        "select": "id,title,publication_year,doi,primary_location,authorships,abstract_inverted_index,cited_by_count,type",
    })
    request = Request(API + "?" + params, headers={"User-Agent": "ai-football-literature-audit/1.0"})
    with urlopen(request, timeout=30) as response:
        return json.load(response).get("results", [])


def source_name(item: dict[str, Any]) -> str:
    location = item.get("primary_location") or {}
    source = location.get("source") or {}
    return str(source.get("display_name") or "")


def authors(item: dict[str, Any]) -> str:
    return "; ".join(str((entry.get("author") or {}).get("display_name") or "")
                     for entry in (item.get("authorships") or [])[:8])


def score(item: dict[str, Any], terms: set[str], *, football_required: bool) -> tuple[int, str]:
    title = str(item.get("title") or "")
    abstract = abstract_text(item.get("abstract_inverted_index"))
    text = (title + " " + abstract).lower()
    hits = sum(1 for term in terms if term in text)
    football_hits = sum(1 for term in FOOTBALL_TERMS if term in text)
    if football_required and football_hits == 0:
        return -1, title
    return hits * 3 + football_hits, title


def collect(queries: tuple[str, ...], terms: set[str], *, football_required: bool) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for query in queries:
        try:
            items = fetch(query)
        except Exception as exc:  # preserve partial research with explicit error log
            ERRORS.append({"query": query, "error": f"{type(exc).__name__}: {exc}"})
            continue
        for item in items:
            identifier = str(item.get("id") or item.get("doi") or "")
            if not identifier:
                continue
            item["_query"] = query
            rank, title = score(item, terms, football_required=football_required)
            item["_rank"] = rank
            item["_title"] = title
            previous = by_id.get(identifier)
            if previous is None or rank > int(previous.get("_rank", -1)):
                by_id[identifier] = item
        time.sleep(0.15)
    selected = sorted(by_id.values(), key=lambda item: (-int(item.get("_rank", -1)),
                                                        -int(item.get("cited_by_count") or 0),
                                                        str(item.get("title") or "")))
    return [item for item in selected if int(item.get("_rank", -1)) >= 0]


def row(item: dict[str, Any], category: str) -> dict[str, Any]:
    abstract = abstract_text(item.get("abstract_inverted_index"))
    excerpt = re.sub(r"\s+", " ", abstract).strip()[:700]
    doi = str(item.get("doi") or "")
    return {
        "category": category,
        "openalex_id": str(item.get("id") or ""),
        "title": re.sub(r"\s+", " ", str(item.get("title") or "")).strip(),
        "year": item.get("publication_year") or "",
        "authors": authors(item),
        "venue": source_name(item),
        "doi": doi.replace("https://doi.org/", ""),
        "url": doi or str(item.get("id") or ""),
        "cited_by_count": item.get("cited_by_count") or 0,
        "query": str(item.get("_query") or ""),
        "relevance_score": item.get("_rank") or 0,
        "abstract_excerpt": excerpt,
        "evidence": "A-元数据/摘要",
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0]) if rows else ["category", "title"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


ERRORS: list[dict[str, str]] = []


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    algorithm = collect(ALGORITHM_QUERIES, ALGORITHM_TERMS, football_required=False)
    model = collect(MODEL_QUERIES, MODEL_TERMS, football_required=True)
    if len(algorithm) < MINIMUM or len(model) < MINIMUM:
        raise SystemExit(f"insufficient literature: algorithm={len(algorithm)} model={len(model)}")
    algorithm_rows = [row(item, "decision_algorithm") for item in algorithm[:max(MINIMUM, 80)]]
    model_rows = [row(item, "decision_model") for item in model[:max(MINIMUM, 80)]]
    write_csv(DATA / "algorithm_papers.csv", algorithm_rows)
    write_csv(DATA / "model_papers.csv", model_rows)
    manifest = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "provider": "OpenAlex works API",
        "algorithm_queries": list(ALGORITHM_QUERIES),
        "model_queries": list(MODEL_QUERIES),
        "algorithm_count": len(algorithm_rows),
        "model_count": len(model_rows),
        "errors": ERRORS,
        "classification": "keyword ranking over title and abstract; manual audit in REPORT.md",
    }
    (DATA / "literature_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
