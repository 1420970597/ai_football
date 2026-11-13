"""Pure, serial window scanning and evidence-driven repair scheduling.

Enumeration, full analysis coverage and repair completion are separate facts.
No failed or partial window may hold the next window hostage.
"""
from __future__ import annotations

PERIOD = 1800
SCAN_BUDGET = 600
MAX_ATTEMPTS = 3
HASH_LENGTH = 64


def is_hash(value):
    return isinstance(value, str) and len(value) == HASH_LENGTH and all(c in "0123456789abcdef" for c in value)


def initial_state(first_start: int) -> dict:
    return {"schema_version": 3, "first_start": first_start, "enumerated_until": first_start,
            "scan_cursor": first_start, "coverage_watermark": first_start,
            "windows": {}, "active_task": None, "legacy_imported": False}


def validate_state(state: dict, first_start: int):
    if state.get("schema_version") != 3 or state.get("first_start") != first_start:
        raise ValueError("schedule version/first window mismatch")
    for key in ("enumerated_until", "scan_cursor", "coverage_watermark"):
        value = state.get(key)
        if type(value) is not int or value < first_start or value % PERIOD:
            raise ValueError("invalid schedule cursor")
    if not isinstance(state.get("windows"), dict):
        raise ValueError("invalid schedule windows")
    for key, entry in state["windows"].items():
        if not key.isdigit() or int(key) < first_start or int(key) % PERIOD:
            raise ValueError("invalid schedule window key")
        if entry.get("start") != int(key) or entry.get("end") != int(key) + PERIOD:
            raise ValueError("invalid schedule window bounds")
        if type(entry.get("scan_attempts")) is not int or entry["scan_attempts"] < 0:
            raise ValueError("invalid scan attempts")


def window_entry(start: int) -> dict:
    return {"start": start, "end": start + PERIOD, "scan_attempts": 0,
            "analysis_status": "pending", "retry_at": 0, "exhausted": False,
            "last_finished_at": None, "last_exit_code": None}


def enumerate_windows(state: dict, now: float):
    start = state["enumerated_until"]
    while start + PERIOD <= now:
        state["windows"].setdefault(str(start), window_entry(start))
        start += PERIOD
    state["enumerated_until"] = start
    update_cursors(state)


def update_cursors(state: dict):
    start, covered = state["first_start"], state["first_start"]
    while str(start) in state["windows"] and (state["windows"][str(start)]["scan_attempts"] > 0 or state["windows"][str(start)].get("manual_verified")):
        start += PERIOD
    while str(covered) in state["windows"] and state["windows"][str(covered)]["analysis_status"] == "complete":
        covered += PERIOD
    state["scan_cursor"], state["coverage_watermark"] = start, covered


def import_legacy(state: dict, records: list[dict], parse_timestamp, now: float):
    """Preserve old files: an interrupted/blocked attempt is never analysis success."""
    if state["legacy_imported"]:
        return
    for record in records:
        start = parse_timestamp(record["window_start"])
        if start < state["first_start"] or start % PERIOD or parse_timestamp(record["window_end"]) != start + PERIOD:
            raise ValueError("invalid legacy window")
        entry = state["windows"].setdefault(str(start), window_entry(start))
        attempts = record.get("attempt", 0)
        if type(attempts) is not int or attempts < 1:
            raise ValueError("invalid legacy attempts")
        complete = record.get("state") == "succeeded" and record.get("exit_code") == 0
        entry.update(scan_attempts=attempts, analysis_status="complete" if complete else "legacy_interrupted"
                     if record.get("state") == "running" else "legacy_failed", exhausted=not complete,
                     last_exit_code=record.get("exit_code"), legacy=True,
                     legacy_state=record.get("state"), legacy_category=record.get("category"),
                     last_finished_at=record.get("finished_at"), imported_at=now)
    state["legacy_imported"] = True
    update_cursors(state)


def choose_task(state: dict, backlog: dict, now: float, scan_timeout=SCAN_BUDGET, repair_timeout=600) -> dict | None:
    if state.get("active_task") is not None:
        return None
    enumerate_windows(state, now)
    entries = [entry for entry in state["windows"].values() if entry["end"] <= now]
    unseen = [entry for entry in entries if entry["scan_attempts"] == 0 and not entry.get("manual_verified")]
    latest = int(now // PERIOD) * PERIOD - PERIOD
    selected = next((entry for entry in unseen if entry["start"] == latest), None)
    if selected:
        # The newest closed half-hour always wins the serial lane.
        return {"kind": "scan", "start": selected["start"], "end": selected["end"],
                "budget_seconds": min(SCAN_BUDGET, scan_timeout), "reason": "new_window"}
    # Never start a fix whose budget would cross the next new scan boundary.
    spare = int((int(now // PERIOD) + 1) * PERIOD - now - 15)
    candidates = [item for item in backlog.values() if item["status"] in {"pending", "retry"}
                  and item["minimal_reproduction"] and item["defect_count"] > 0
                  and item["attempts"] < MAX_ATTEMPTS and item["retry_at"] <= now]
    if candidates and spare >= 60:
        item = min(candidates, key=lambda e: (e.get("last_dispatch_at", 0), e["created_at"], e["issue_key"]))
        return {"kind": "repair", "start": item["window_start"], "end": item["window_start"] + PERIOD,
                "issue_key": item["issue_key"], "evidence_fingerprint": item["evidence_fingerprint"],
                "analysis_attempt": item["analysis_attempt"], "budget_seconds": min(repair_timeout, spare), "reason": "confirmed_defect"}
    if unseen:
        selected = min(unseen, key=lambda e: e["start"])
        return {"kind": "scan", "start": selected["start"], "end": selected["end"],
                "budget_seconds": min(SCAN_BUDGET, scan_timeout), "reason": "catchup"}
    retries = [entry for entry in entries if entry["analysis_status"] in {"failed", "partial", "interrupted"}
               and not entry["exhausted"] and entry["retry_at"] <= now and entry["scan_attempts"] < MAX_ATTEMPTS]
    if retries:
        selected = min(retries, key=lambda e: (e.get("last_dispatch_at", 0), e["start"]))
        return {"kind": "scan", "start": selected["start"], "end": selected["end"],
                "budget_seconds": min(SCAN_BUDGET, scan_timeout), "reason": "bounded_retry"}
    return None


def reserve_task(state: dict, backlog: dict, task: dict, now: float, token: str):
    if state.get("active_task"):
        raise ValueError("a task already owns the serial lane")
    task = {**task, "token": token, "started_at": now, "deadline_at": now + task["budget_seconds"]}
    if task["kind"] == "scan":
        entry = state["windows"][str(task["start"])]
        entry["scan_attempts"] += 1
        entry.update(analysis_status="running", last_dispatch_at=now)
    else:
        item = backlog[task["issue_key"]]
        item["attempts"] += 1
        item.update(status="running", last_dispatch_at=now)
    state["active_task"] = task
    update_cursors(state)


def register_repairs(backlog: dict, candidates: list[dict], task: dict, record: dict, now: float):
    for candidate in candidates:
        if not candidate["minimal_reproduction"] or candidate["defect_count"] < 1:
            continue
        key, fingerprint = candidate["issue_key"], candidate["evidence_fingerprint"]
        old = backlog.get(key)
        if old is not None and old["evidence_fingerprint"] == fingerprint:
            # The same external block cannot reopen itself through new docs/HEAD.
            old["last_observed_at"] = now
            continue
        backlog[key] = {**candidate, "window_start": task["start"], "analysis_attempt": record["attempt"],
                        "attempts": 0, "retry_at": now, "created_at": old["created_at"] if old else now,
                        "last_observed_at": now, "status": "pending",
                        "prior_fingerprints": [*(old.get("prior_fingerprints", []) if old else []),
                                               *([old["evidence_fingerprint"]] if old else [])]}


def finish_task(state: dict, backlog: dict, record: dict, now: float, retry_seconds=300):
    task = state.get("active_task")
    if not task or record.get("task_token") != task["token"]:
        raise ValueError("result does not own the active task")
    result = record.get("result") or {}
    if task["kind"] == "scan":
        entry = state["windows"][str(task["start"])]
        outcome = result.get("analysis_status") if record.get("result_valid") else None
        outcome = outcome if outcome in {"complete", "partial", "failed"} else "interrupted" if record.get("exit_code") == 130 else "failed"
        stable_partial = outcome == "partial" and record.get("result_valid") and result.get("retryable") is False
        entry.update(analysis_status=outcome, last_finished_at=record.get("finished_at"), last_exit_code=record.get("exit_code"),
                     last_category=record.get("category"), last_attempt=record.get("attempt"),
                     retry_at=now + min(PERIOD, retry_seconds * 2 ** (entry["scan_attempts"] - 1)),
                     exhausted=stable_partial or outcome != "complete" and entry["scan_attempts"] >= MAX_ATTEMPTS)
        if record.get("result_valid"):
            entry["blocker_codes"] = result.get("blocker_codes", [])
            register_repairs(backlog, result.get("repair_candidates", []), task, record, now)
        state["last_scan"] = {"start": task["start"], "end": task["end"], "analysis_status": outcome,
                              "messages_examined": result.get("messages_examined"),
                              "unresolved_examined": result.get("unresolved_examined"),
                              "blocker_codes": result.get("blocker_codes", []), "attempt": record.get("attempt")}
    else:
        item = backlog[task["issue_key"]]
        validated = record.get("result_valid") and result.get("repair_status") == "validated"
        retryable = record.get("result_valid") and result.get("retryable") is True
        item.update(status="validated" if validated else "exhausted" if item["attempts"] >= MAX_ATTEMPTS else "retry" if retryable else "waiting_evidence",
                    retry_at=now + min(PERIOD, retry_seconds * 2 ** (item["attempts"] - 1)),
                    last_finished_at=record.get("finished_at"), last_exit_code=record.get("exit_code"),
                    blocker_codes=result.get("blocker_codes", []), last_category=record.get("category"))
    state["active_task"] = None
    state["last_finished_at"] = now
    update_cursors(state)


def recover_interrupted(state: dict, backlog: dict, now: float, retry_seconds=300):
    task = state.get("active_task")
    if task:
        finish_task(state, backlog, {"task_token": task["token"], "exit_code": 130, "finished_at": now,
                    "category": "supervisor_restarted_during_task", "result_valid": False}, now, retry_seconds)
    for item in backlog.values():
        if item["status"] == "running":
            item.update(status="waiting_evidence", last_category="supervisor_restarted_during_repair")


def health_snapshot(state: dict, backlog: dict, now: float, heartbeat: float) -> dict:
    entries = list(state["windows"].values())
    unseen = [e for e in entries if e["scan_attempts"] == 0 and not e.get("manual_verified")]
    failures = [e for e in entries if e["analysis_status"] in {"failed", "interrupted", "legacy_failed", "legacy_interrupted"}]
    partial = [e for e in entries if e["analysis_status"] == "partial"]
    lag = max(0, int(now // PERIOD) * PERIOD - state["coverage_watermark"])
    active = state.get("active_task")
    alive = now - heartbeat <= 90 and (active is None or now <= active["deadline_at"] + 10)
    coverage_ok = not failures and not partial and not any(e["end"] + SCAN_BUDGET < now for e in unseen) and lag <= PERIOD
    return {"schema_version": 3, "state": "running" if active else "waiting", "heartbeat_at": heartbeat,
            "process_healthy": alive, "coverage_healthy": coverage_ok, "healthy": alive and coverage_ok,
            "enumerated_until": state["enumerated_until"], "scan_cursor": state["scan_cursor"],
            "coverage_watermark": state["coverage_watermark"], "coverage_lag_seconds": lag,
            "unscanned_windows": len(unseen), "failed_windows": len(failures), "partial_windows": len(partial),
            "exhausted_windows": sum(e["exhausted"] for e in entries),
            "repair_pending": sum(e["status"] != "validated" for e in backlog.values()),
            "repair_waiting_evidence": sum(e["status"] in {"waiting_evidence", "exhausted"} for e in backlog.values()),
            "active_task": {k: active[k] for k in ("kind", "start", "end", "deadline_at")} if active else None}
