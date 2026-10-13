#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect a reproducible, two-track open-source project catalogue from GitHub.

Track A (decision algorithm): betting value/staking/Kelly, devig & implied
probability, calibration, ensembles, online learning, risk/portfolio, time
validation and market-efficiency tooling.

Track B (decision model): football/soccer outcome, Poisson/Dixon-Coles,
goal/scoreline, xG, in-play and rating-system models plus their data layers.

Selection is deterministic: fixed queries -> relevance score over
name/description/topics -> de-duplicate -> rank -> cap.  Metadata is the
evidence; this script does not clone or execute any repository.
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
API = "https://api.github.com/search/repositories"
PER_PAGE = 100
CANDIDATE_POOL = 500
MIN_STARS = 3
SELECT_PER_TRACK = 100
REQUEST_PAUSE_S = 0.7

ALGORITHM_QUERIES = (
    "kelly criterion betting",
    "sports betting model",
    "value betting odds",
    "implied probability devig odds",
    "sports betting arbitrage",
    "probability calibration",
    "conformal prediction",
    "ensemble forecast combination",
    "online learning concept drift",
    "risk constrained portfolio optimization",
    "sports odds api",
    "backtesting framework strategy",
)

MODEL_QUERIES = (
    "football prediction model",
    "soccer match prediction",
    "football poisson model goals",
    "dixon coles model",
    "expected goals xg",
    "football machine learning prediction",
    "in-play football prediction",
    "football betting prediction",
    "soccer analytics",
    "elo rating football",
    "football event data",
    "soccer scoreline prediction",
)

ALGORITHM_TERMS = {
    "bet": 3, "betting": 3, "wager": 3, "kelly": 4, "stake": 2, "staking": 3,
    "odds": 3, "bookmaker": 3, "arbitrage": 3, "value": 1, "implied": 2,
    "probability": 1, "probabilities": 1, "calibrat": 3, "conformal": 3,
    "ensemble": 2, "forecast": 2, "drift": 2, "online-learning": 2,
    "portfolio": 2, "risk": 1, "backtest": 3, "backtesting": 3, "ev": 1,
    "expected-value": 3, "market-efficiency": 3, "efficien": 1, "trading": 1,
    "quant": 1, "exchange": 1, "bankroll": 3, "pricing": 1, "decision": 1,
    "uncertainty": 1, "bayesian": 1, "monte-carlo": 1, "simulation": 1,
    "scoring": 1, "rating": 1, "sportsbook": 3, "pinnacle": 3, "betfair": 3,
}

MODEL_TERMS = {
    "football": 3, "soccer": 3, "match": 1, "goals": 2, "goal": 1,
    "poisson": 4, "dixon-coles": 5, "dixon": 3, "coles": 3, "score": 1,
    "scoreline": 3, "prediction": 1, "predict": 1, "outcome": 1, "xg": 4,
    "expected-goals": 4, "in-play": 3, "live": 1, "elo": 3, "rating": 1,
    "team": 1, "league": 1, "premier-league": 1, "world-cup": 1, "euro": 1,
    "statsbomb": 3, "understat": 2, "fbref": 2, "event-data": 3, "events": 1,
    "bayesian": 1, "machine-learning": 1, "deep-learning": 1, "lstm": 1,
    "gradient-boosting": 1, "random-forest": 1, "neural": 1, "hazard": 2,
    "survival": 1, "bivariate": 2, "bradley-terry": 2, "massey": 2,
    "analytics": 1, "match-prediction": 3, "score-prediction": 3,
}

TRACKS = {
    "algorithm": (ALGORITHM_QUERIES, ALGORITHM_TERMS,
                  "oss_algorithm_projects.csv"),
    "model": (MODEL_QUERIES, MODEL_TERMS, "oss_model_projects.csv"),
}

STOPWORDS = {"the", "a", "an", "of", "and", "or", "for", "to", "in", "on",
             "with", "using", "based", "data", "api", "python", "model"}


def github_token() -> str:
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True,
                             text=True, timeout=15)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def request_json(url: str, token: str, retries: int = 3) -> dict:
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "ai-football-literature-audit"}
    if token:
        headers["Authorization"] = "Bearer " + token
    last_error: BaseException | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in (403, 429):
                wait = 20 + 20 * attempt
                print("rate limited (%s), sleeping %ss" % (exc.code, wait))
                time.sleep(wait)
                continue
            raise
        except Exception as exc:  # timeout / connection
            last_error = exc
            time.sleep(5 + 5 * attempt)
    raise RuntimeError("request failed: %s" % last_error)


def score_repo(repo: dict, terms: dict) -> int:
    haystack = " ".join((
        (repo.get("name") or "").lower(),
        (repo.get("description") or "").lower(),
        " ".join(repo.get("topics") or []).lower(),
    ))
    score = 0
    for term, weight in terms.items():
        if term in haystack:
            score += weight
    return score


def collect_track(kind: str, token: str) -> tuple[list[dict], list[dict]]:
    queries, terms, _ = TRACKS[kind]
    by_name: dict[str, dict] = {}
    fetched: list[dict] = []
    for query in queries:
        q = '%s stars:>=%d' % (query, MIN_STARS)
        params = urllib.parse.urlencode({
            "q": q, "sort": "stars", "order": "desc", "per_page": PER_PAGE})
        payload = request_json(API + "?" + params, token)
        items = payload.get("items") or []
        fetched.append({"query": query, "total_count": payload.get("total_count"),
                        "returned": len(items)})
        for repo in items:
            name = repo.get("full_name")
            if not name or name in by_name:
                continue
            score = score_repo(repo, terms)
            if score <= 0:
                continue
            by_name[name] = {
                "track": kind,
                "full_name": name,
                "url": repo.get("html_url"),
                "description": (repo.get("description") or "").strip(),
                "stars": repo.get("stargazers_count", 0),
                "forks": repo.get("forks_count", 0),
                "open_issues": repo.get("open_issues_count", 0),
                "language": repo.get("language") or "",
                "license": (repo.get("license") or {}).get("spdx_id") or "",
                "created_at": (repo.get("created_at") or "")[:10],
                "pushed_at": (repo.get("pushed_at") or "")[:10],
                "archived": bool(repo.get("archived")),
                "topics": ",".join(repo.get("topics") or []),
                "relevance_score": score,
                "discovery_queries": query,
            }
        print("  [%s] %-45s -> %d candidates" % (kind, query, len(by_name)))
        time.sleep(REQUEST_PAUSE_S)
    ranked = sorted(by_name.values(),
                    key=lambda r: (-r["relevance_score"] * 1000 - r["stars"]))
    selected = ranked[:SELECT_PER_TRACK]
    return selected, fetched


def main() -> int:
    token = github_token()
    if not token:
        print("warning: no GitHub token; unauthenticated limits apply",
              file=sys.stderr)
    DATA.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "provider": "GitHub search repositories API",
        "authenticated": bool(token),
        "min_stars": MIN_STARS,
        "select_per_track": SELECT_PER_TRACK,
        "tracks": {},
        "errors": [],
    }
    for kind in ("algorithm", "model"):
        selected, fetched = collect_track(kind, token)
        _, _, filename = TRACKS[kind]
        fields = list(selected[0].keys()) if selected else [
            "track", "full_name", "url", "description", "stars", "forks",
            "open_issues", "language", "license", "created_at", "pushed_at",
            "archived", "topics", "relevance_score", "discovery_queries"]
        with (DATA / filename).open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(selected)
        manifest["tracks"][kind] = {
            "queries": list(TRACKS[kind][0]), "fetched": fetched,
            "candidates": len(selected), "saved": filename,
            "top_by_stars": [r["full_name"] for r in
                             sorted(selected, key=lambda r: -r["stars"])[:5]],
        }
        print("[%s] saved %d projects -> %s" % (kind, len(selected), filename))
    with (DATA / "oss_manifest.json").open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
    print("manifest -> data/oss_manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
