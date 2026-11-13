"""Cadence recovery contracts, including the real attempt54 failure shape."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from infra.monitor import scan_scheduler as schedule
from infra.monitor.expert_iteration_supervisor import (
    Config, atomic_json, command, health_check, import_analysis, load_schedule, main, make_prompt,
    parse_timestamp, run_child, run_window, save_schedule, utc_timestamp, validate_task_result,
)


class ExpertIterationSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.private = self.root / ".runtime" / "iteration"
        self.private.mkdir(parents=True)
        self.first = parse_timestamp("2026-11-12T13:00:00Z")
        self.config = Config(self.source, self.private, self.first, project_root=self.root)
        self.state = schedule.initial_state(self.first)
        self.state["legacy_imported"] = True
        self.backlog = {}
        self.now = self.first + 21 * schedule.PERIOD + 120
        schedule.enumerate_windows(self.state, self.now)
        self.number = 0

    def candidate(self, fingerprint="b" * 64, minimum=True):
        return {"issue_key": "a" * 64, "evidence_fingerprint": fingerprint,
                "minimal_reproduction": minimum, "defect_count": 1}

    def result(self, task, **changes):
        return {"window_start": utc_timestamp(task["start"]), "window_end": utc_timestamp(task["end"]),
                "task_kind": task["kind"], "status": "complete", "analysis_status": "complete" if task["kind"] == "scan" else "not_run",
                "repair_status": "not_needed" if task["kind"] == "scan" else "blocked",
                "issue_key": task.get("issue_key", ""), "evidence_fingerprint": task.get("evidence_fingerprint", ""),
                "messages_examined": 3, "unresolved_examined": 1, "confirmed_omissions": 1,
                "confirmed_misbindings": 0, "retryable": False, "validation_passed": False,
                "incremental_evidence": True, "latency_target_met": False, "summary": "no raw text",
                "blocker_codes": [], "repair_candidates": [], "fix_commits": [], **changes}

    def finish(self, task, result=None, valid=True, exit_code=0, when=None):
        self.number += 1
        when = self.now if when is None else when
        schedule.reserve_task(self.state, self.backlog, task, when, f"token-{self.number}")
        active = self.state["active_task"]
        record = {"task_token": active["token"], "attempt": self.number, "result_valid": valid,
                  "result": result or self.result(task), "exit_code": exit_code,
                  "finished_at": utc_timestamp(when), "category": "test_outcome"}
        schedule.finish_task(self.state, self.backlog, record, when)
        return record

    def scan_all(self, result_changes=None):
        visited = []
        while True:
            task = schedule.choose_task(self.state, self.backlog, self.now)
            if task is None or task["kind"] != "scan" or task["reason"] == "bounded_retry":
                break
            visited.append(task["start"])
            self.finish(task, self.result(task, **(result_changes or {})))
        return visited

    def test_latest_new_window_then_oldest_catchup_without_skipping_any_slot(self):
        visited = self.scan_all()
        expected = list(range(self.first, self.first + 21 * schedule.PERIOD, schedule.PERIOD))
        self.assertEqual(expected[-1], visited[0])
        self.assertEqual(expected[:-1], visited[1:])
        self.assertEqual(set(expected), set(visited))
        self.assertEqual(self.first + 21 * schedule.PERIOD, self.state["coverage_watermark"])

    def test_new_half_hour_preempts_existing_catchup(self):
        old = schedule.choose_task(self.state, self.backlog, self.now)
        self.finish(old)
        task = schedule.choose_task(self.state, self.backlog, self.now + schedule.PERIOD)
        self.assertEqual(old["start"] + schedule.PERIOD, task["start"])
        self.assertEqual("new_window", task["reason"])

    def test_partial_media_and_failed_scans_leave_gaps_but_do_not_block_new_slots(self):
        latest = schedule.choose_task(self.state, self.backlog, self.now)
        self.finish(latest, self.result(latest, analysis_status="partial", repair_status="blocked", blocker_codes=["media_unavailable"]))
        second = schedule.choose_task(self.state, self.backlog, self.now)
        self.assertEqual(self.first, second["start"])
        self.finish(second, valid=False, exit_code=9)
        third = schedule.choose_task(self.state, self.backlog, self.now)
        self.assertEqual(self.first + schedule.PERIOD, third["start"])
        self.assertEqual(self.first, self.state["coverage_watermark"])

    def test_analysis_complete_is_coverage_even_when_repair_is_blocked(self):
        self.state = schedule.initial_state(self.first)
        schedule.enumerate_windows(self.state, self.first + schedule.PERIOD)
        task = schedule.choose_task(self.state, self.backlog, self.first + schedule.PERIOD)
        result = self.result(task, status="blocked", repair_status="blocked", blocker_codes=["release_blocked"])
        self.finish(task, result)
        self.assertEqual(self.first + schedule.PERIOD, self.state["coverage_watermark"])
        self.assertEqual("complete", self.state["windows"][str(self.first)]["analysis_status"])

    def test_stable_partial_keeps_health_gap_without_retrying_identical_media(self):
        task = schedule.choose_task(self.state, self.backlog, self.now)
        self.finish(task, self.result(task, analysis_status="partial", retryable=False,
                                      blocker_codes=["media_unavailable"]))
        self.scan_all()
        later = self.now + 600
        self.assertIsNone(schedule.choose_task(self.state, self.backlog, later))
        entry = self.state["windows"][str(task["start"])]
        self.assertEqual(1, entry["scan_attempts"])
        self.assertTrue(entry["exhausted"])
        health = schedule.health_snapshot(self.state, self.backlog, later, later)
        self.assertFalse(health["healthy"])
        self.assertEqual(1, health["partial_windows"])

    def test_confirmed_repair_beats_twenty_catchups_but_new_window_still_wins(self):
        latest = schedule.choose_task(self.state, self.backlog, self.now)
        self.finish(latest, self.result(latest, repair_candidates=[self.candidate()]))
        self.assertEqual(20, sum(entry["scan_attempts"] == 0 for entry in self.state["windows"].values()))
        repair = schedule.choose_task(self.state, self.backlog, self.now)
        self.assertEqual("repair", repair["kind"])
        self.assertEqual("confirmed_defect", repair["reason"])
        boundary = (int(self.now // schedule.PERIOD) + 1) * schedule.PERIOD
        fresh = schedule.choose_task(self.state, self.backlog, boundary)
        self.assertEqual("scan", fresh["kind"])
        self.assertEqual("new_window", fresh["reason"])
        self.assertEqual(boundary - schedule.PERIOD, fresh["start"])

    def test_legacy_53_failures_and_running_attempt54_are_not_imported_as_success(self):
        self.state = schedule.initial_state(self.first)
        old = {"window_start": utc_timestamp(self.first), "window_end": utc_timestamp(self.first + schedule.PERIOD),
               "attempt": 54, "state": "running", "exit_code": None, "finished_at": None}
        schedule.import_legacy(self.state, [old], parse_timestamp, self.now)
        schedule.enumerate_windows(self.state, self.now)
        entry = self.state["windows"][str(self.first)]
        self.assertEqual("legacy_interrupted", entry["analysis_status"])
        self.assertEqual(54, entry["scan_attempts"])
        self.assertTrue(entry["exhausted"])
        self.assertEqual(self.first, self.state["coverage_watermark"])
        self.assertEqual(self.first + schedule.PERIOD, self.state["scan_cursor"])
        visited = self.scan_all()
        for start in range(parse_timestamp("2026-11-12T17:00:00Z"), parse_timestamp("2026-11-12T19:00:00Z"), schedule.PERIOD):
            self.assertIn(start, visited)
        self.assertNotIn(self.first, visited)

    def test_real_shape_upgrade_reads_old_history_without_modifying_evidence(self):
        old = {"window_start": utc_timestamp(self.first), "window_end": utc_timestamp(self.first + schedule.PERIOD),
               "attempt": 54, "state": "running", "exit_code": None, "finished_at": None}
        atomic_json(self.private / "windows" / f"{self.first}.json", old)
        for attempt in range(1, 54):
            atomic_json(self.private / "history" / f"{self.first}-{attempt}.json", {"attempt": attempt, "state": "failed"})
        before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.private.rglob("*.json")}
        state, backlog = load_schedule(self.config, self.now)
        self.assertEqual(53, state["legacy_history_records"])
        self.assertEqual("legacy_interrupted", state["windows"][str(self.first)]["analysis_status"])
        after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.private.rglob("*.json")}
        self.assertEqual(before, after)
        self.assertEqual({}, backlog)
        self.assertFalse((self.private / "progress.json").exists())

    def test_failed_scans_have_three_attempt_cap_and_exponential_cooldown(self):
        self.state = schedule.initial_state(self.first)
        now = self.first + schedule.PERIOD + 5
        schedule.enumerate_windows(self.state, now)
        for attempt in range(1, 4):
            task = schedule.choose_task(self.state, self.backlog, now)
            self.assertIsNotNone(task)
            self.finish(task, valid=False, exit_code=124, when=now)
            entry = self.state["windows"][str(self.first)]
            self.assertEqual(attempt, entry["scan_attempts"])
            self.assertEqual(now + 300 * 2 ** (attempt - 1), entry["retry_at"])
            self.assertIsNone(schedule.choose_task(self.state, self.backlog, now))
            now = entry["retry_at"]
        self.assertTrue(entry["exhausted"])
        # Even far in the future, fresh slots win; the old failure is not reset.
        selected = schedule.choose_task(self.state, self.backlog, now + 10000)
        self.assertNotEqual(self.first, selected["start"])

    def test_failure_retry_rotates_instead_of_permanently_repeating_oldest(self):
        self.scan_all({"analysis_status": "failed"})
        now = self.now + 300
        first = schedule.choose_task(self.state, self.backlog, now)
        self.finish(first, self.result(first, analysis_status="failed"), when=now)
        second = schedule.choose_task(self.state, self.backlog, now)
        self.assertNotEqual(first["start"], second["start"])

    def test_confirmed_repair_needs_minimum_reproduction_and_separate_serial_lane(self):
        self.scan_all()
        task = {"kind": "scan", "start": self.first, "end": self.first + schedule.PERIOD, "budget_seconds": 600}
        self.finish(task, self.result(task, repair_candidates=[self.candidate(minimum=False)]))
        self.assertEqual({}, self.backlog)
        self.finish(task, self.result(task, repair_candidates=[self.candidate()]))
        repair = schedule.choose_task(self.state, self.backlog, self.now)
        self.assertEqual("repair", repair["kind"])
        schedule.reserve_task(self.state, self.backlog, repair, self.now, "repair-one")
        self.assertIsNone(schedule.choose_task(self.state, self.backlog, self.now))

    def test_stable_external_block_waits_for_new_evidence_not_new_docs_or_clock(self):
        self.scan_all()
        task = {"kind": "scan", "start": self.first, "end": self.first + schedule.PERIOD, "budget_seconds": 600}
        self.finish(task, self.result(task, repair_candidates=[self.candidate()]))
        repair = schedule.choose_task(self.state, self.backlog, self.now)
        self.finish(repair, self.result(repair, repair_status="blocked", retryable=False, blocker_codes=["upstream_unavailable"]))
        item = self.backlog["a" * 64]
        self.assertEqual("waiting_evidence", item["status"])
        self.finish(task, self.result(task, repair_candidates=[self.candidate()]))
        self.assertEqual(1, item["attempts"])
        self.assertEqual("waiting_evidence", item["status"])
        self.finish(task, self.result(task, repair_candidates=[self.candidate("c" * 64)]))
        self.assertEqual("pending", self.backlog["a" * 64]["status"])
        self.assertEqual(0, self.backlog["a" * 64]["attempts"])

    def test_repair_budget_cannot_cross_next_scan_and_new_scan_always_wins(self):
        self.scan_all()
        task = {"kind": "scan", "start": self.first, "end": self.first + schedule.PERIOD, "budget_seconds": 600}
        self.finish(task, self.result(task, repair_candidates=[self.candidate()]))
        boundary = (int(self.now // schedule.PERIOD) + 1) * schedule.PERIOD
        self.assertIsNone(schedule.choose_task(self.state, self.backlog, boundary - 50))
        repair = schedule.choose_task(self.state, self.backlog, boundary - 100)
        self.assertEqual(85, repair["budget_seconds"])
        fresh = schedule.choose_task(self.state, self.backlog, boundary)
        self.assertEqual("scan", fresh["kind"])
        self.assertEqual("new_window", fresh["reason"])

    def test_retryable_repairs_also_have_three_attempt_cap(self):
        self.scan_all()
        task = {"kind": "scan", "start": self.first, "end": self.first + schedule.PERIOD, "budget_seconds": 600}
        self.finish(task, self.result(task, repair_candidates=[self.candidate()]))
        now = self.now
        for _ in range(3):
            repair = schedule.choose_task(self.state, self.backlog, now)
            if repair["kind"] == "scan":
                self.finish(repair, when=now)
                repair = schedule.choose_task(self.state, self.backlog, now)
            self.finish(repair, self.result(repair, repair_status="failed", retryable=True), when=now)
            now = self.backlog["a" * 64]["retry_at"]
        self.assertEqual("exhausted", self.backlog["a" * 64]["status"])

    def test_running_heartbeat_does_not_hide_lag_failures_or_partial_analysis(self):
        health = schedule.health_snapshot(self.state, self.backlog, self.now, self.now)
        self.assertTrue(health["process_healthy"])
        self.assertFalse(health["healthy"])
        self.assertEqual(21, health["unscanned_windows"])
        task = schedule.choose_task(self.state, self.backlog, self.now)
        schedule.reserve_task(self.state, self.backlog, task, self.now, "active")
        self.assertFalse(schedule.health_snapshot(self.state, self.backlog, self.now, self.now)["healthy"])
        self.assertFalse(schedule.health_snapshot(self.state, self.backlog, self.now + 1000, self.now + 1000)["process_healthy"])

    def test_health_cli_rechecks_stale_heartbeat_and_task_deadline(self):
        self.scan_all()
        status = schedule.health_snapshot(self.state, self.backlog, self.now, self.now)
        atomic_json(self.private / "status.json", status)
        self.assertEqual(0, health_check(self.config, self.now))
        self.assertEqual(1, health_check(self.config, self.now + 91))
        status["active_task"] = {"deadline_at": self.now - 11}
        atomic_json(self.private / "status.json", status)
        self.assertEqual(1, health_check(self.config, self.now))

    def test_crash_after_finished_journal_replays_result_once_without_new_child(self):
        task = schedule.choose_task(self.state, self.backlog, self.now)
        schedule.reserve_task(self.state, self.backlog, task, self.now, "journal-one")
        save_schedule(self.config, self.state, self.backlog)
        record = {"task_token": "journal-one", "attempt": 1, "finished_at": utc_timestamp(self.now),
                  "result_valid": True, "exit_code": 0, "result": self.result(task)}
        atomic_json(self.private / "windows" / f"{task['start']}.json", record)
        state, backlog = load_schedule(self.config, self.now)
        self.assertIsNone(state["active_task"])
        self.assertEqual("complete", state["windows"][str(task["start"])]["analysis_status"])
        self.assertEqual(1, state["windows"][str(task["start"])]["scan_attempts"])
        save_schedule(self.config, state, backlog)
        state2, _ = load_schedule(self.config, self.now)
        self.assertEqual(1, state2["windows"][str(task["start"])]["scan_attempts"])

    def test_restart_without_final_journal_preserves_interrupted_gap(self):
        task = schedule.choose_task(self.state, self.backlog, self.now)
        schedule.reserve_task(self.state, self.backlog, task, self.now, "interrupted")
        save_schedule(self.config, self.state, self.backlog)
        state, _ = load_schedule(self.config, self.now)
        entry = state["windows"][str(task["start"])]
        self.assertEqual("interrupted", entry["analysis_status"])
        self.assertEqual(130, entry["last_exit_code"])
        self.assertEqual(self.first, schedule.choose_task(state, {}, self.now)["start"])

    def test_complete_analysis_with_blocked_repair_is_valid_new_schema(self):
        task = schedule.choose_task(self.state, self.backlog, self.now)
        path = self.private / "result.json"
        path.write_text(json.dumps(self.result(task, status="blocked", repair_status="blocked")))
        valid, _, value = validate_task_result(path, task)
        self.assertTrue(valid)
        self.assertEqual("complete", value["analysis_status"])

    def test_scan_prompt_forbids_history_planning_and_git_mutations(self):
        text = make_prompt(self.config, self.first, self.first + schedule.PERIOD, self.private)
        for phrase in ("本轮只读扫描", "禁止改业务", "Git提交/推送", "不遍历数十篇旧报告", "不得重复生成planning", "issue_key"):
            self.assertIn(phrase, text)

    def test_task_stop_records_exit130_and_does_not_erase_history(self):
        task = {**schedule.choose_task(self.state, self.backlog, self.now), "token": "stop"}
        def stop(*_):
            raise KeyboardInterrupt
        record = run_window(self.config, task["start"], task["end"], runner=stop, task=task)
        self.assertEqual(130, record["exit_code"])
        self.assertEqual("interrupted", record["state"])
        self.assertTrue((self.private / "history" / f"{task['start']}-1.json").exists())

    def test_scan_cannot_claim_success_after_a_git_commit(self):
        task = {**schedule.choose_task(self.state, self.backlog, self.now), "token": "commit-guard"}
        def runner(config, prompt, directory):
            (directory / "final.json").write_text(json.dumps(self.result(task)))
            return 0, False
        with patch("infra.monitor.expert_iteration_supervisor.repository_revision", side_effect=["1" * 40, "2" * 40]):
            record = run_window(self.config, task["start"], task["end"], runner=runner, task=task)
        self.assertFalse(record["result_valid"])
        self.assertEqual("scan_changed_repository", record["category"])

    def test_documentation_only_commit_cannot_validate_a_repair(self):
        task = {"kind": "repair", "start": self.first, "end": self.first + schedule.PERIOD,
                "token": "doc-guard", "issue_key": "a" * 64, "evidence_fingerprint": "b" * 64, "analysis_attempt": 1}
        def runner(config, prompt, directory):
            result = self.result(task, repair_status="validated", validation_passed=True, fix_commits=["2" * 40])
            (directory / "final.json").write_text(json.dumps(result))
            return 0, False
        with patch("infra.monitor.expert_iteration_supervisor.repository_revision", side_effect=["1" * 40, "2" * 40]), \
             patch("infra.monitor.expert_iteration_supervisor.has_source_commit", return_value=False):
            record = run_window(self.config, self.first, self.first + schedule.PERIOD, runner=runner, task=task)
        self.assertFalse(record["result_valid"])
        self.assertEqual("repair_has_no_source_commit", record["category"])

    def test_real_child_timeout_is_bounded_and_returns124_without_model(self):
        config = replace(self.config, timeout_seconds=1)
        started = time.monotonic()
        with patch("infra.monitor.expert_iteration_supervisor.command", return_value=[sys.executable, "-c", "import time;time.sleep(60)"]):
            self.assertEqual((124, True), run_child(config, "", self.private))
        self.assertLess(time.monotonic() - started, 3)

    def test_task_absolute_deadline_includes_preparation_and_never_starts_expired_child(self):
        task = {**schedule.choose_task(self.state, self.backlog, self.now), "token": "expired", "deadline_at": self.now - 1}
        def unexpected(*_):
            self.fail("An expired task must not launch a new child")
        record = run_window(self.config, task["start"], task["end"], clock=lambda: self.now, runner=unexpected, task=task)
        self.assertEqual(124, record["exit_code"])
        self.assertTrue(record["timed_out"])

    def test_real_main_runs_next_windows_serially_with_a_local_fake_cli(self):
        # This is a local protocol fixture, not a model or a business replay.
        executable = self.root / "fake-codex"
        base = self.result({"kind": "scan", "start": self.first, "end": self.first + schedule.PERIOD})
        program = (f"#!{sys.executable}\nimport json,re,sys,pathlib\n"
                   f"result={base!r}\n"
                   "prompt=sys.stdin.read()\n"
                   "windows=re.search(r'\\[([^,]+Z), ([^\\)]+Z)\\)',prompt)\n"
                   "result['window_start'],result['window_end']=windows.groups()\n"
                   "pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(result))\n"
                   "print(json.dumps({'type':'turn.completed'}))\n")
        executable.write_text(program)
        executable.chmod(0o700)
        def fixture_command(config, final_path):
            # Docker /tmp stays noexec; run this test-only script through the
            # existing interpreter rather than weakening that mount policy.
            return [sys.executable, str(executable), *command(config, final_path)[1:]]
        values = {"EXPERT_PROJECT_ROOT": str(self.root), "EXPERT_SOURCE_ROOT": str(self.source),
                  "EXPERT_ITERATION_STATE_DIR": str(self.private), "EXPERT_CODEX_BINARY": str(executable),
                  "EXPERT_ITERATION_FIRST_START": utc_timestamp(self.first)}
        for _ in range(3):
            with patch.dict("os.environ", values), patch("sys.argv", ["supervisor", "--once"]), \
                    patch("infra.monitor.expert_iteration_supervisor.time.time", return_value=self.now), \
                    patch("infra.monitor.expert_iteration_supervisor.command", side_effect=fixture_command), \
                    patch("sys.stdout", new_callable=io.StringIO):
                code = main()
                records = [json.loads(path.read_text()) for path in (self.private / "windows").glob("*.json")]
                self.assertEqual(0, code, records)
        state = json.loads((self.private / "schedule-v3.json").read_text())
        complete = [entry for entry in state["windows"].values() if entry["analysis_status"] == "complete"]
        self.assertEqual(3, len(complete))
        self.assertEqual(3, len(list((self.private / "history").glob("*.json"))))
        self.assertEqual(1, state["windows"][str(self.first)]["scan_attempts"])
        self.assertEqual(1, state["windows"][str(self.first + schedule.PERIOD)]["scan_attempts"])
        self.assertEqual(1, health_check(self.config, self.now))

    def test_manual_analysis_import_requires_real_matching_proof_hash(self):
        proof = self.root / "verified-summary.json"
        proof.write_text('{"analysis":"operator reviewed actual samples"}')
        payload = self.root / "import.json"
        item = {"window_start": utc_timestamp(self.first), "window_end": utc_timestamp(self.first + schedule.PERIOD),
                "analysis_status": "complete", "messages_examined": 3, "unresolved_examined": 1,
                "evidence_file": str(proof), "evidence_sha256": hashlib.sha256(proof.read_bytes()).hexdigest()}
        payload.write_text(json.dumps({"schema_version": 1, "windows": [item]}))
        import_analysis(self.config, self.state, payload, self.now)
        entry = self.state["windows"][str(self.first)]
        self.assertTrue(entry["manual_verified"])
        self.assertEqual(0, entry["scan_attempts"])
        self.assertEqual(self.first + schedule.PERIOD, self.state["coverage_watermark"])
        proof.write_text("tampered")
        with self.assertRaises(ValueError):
            import_analysis(self.config, self.state, payload, self.now)


if __name__ == "__main__":
    unittest.main()
