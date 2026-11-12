from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import sys
import unittest
from unittest.mock import patch

from infra.monitor.expert_iteration_supervisor import (
    AlreadyRunning, Config, MAX_EVENT_BYTES, PERIOD_SECONDS, RAW_RETENTION_SECONDS, RedactedOutput, acquire_lock,
    atomic_json, command, commit_success, load_progress, make_prompt, next_window,
    parse_timestamp, prune_private_evidence, recover_recorded_success, redact_final, run_child, run_window,
    utc_timestamp, validate_result,
)


class ExpertIterationSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.source = self.project / "source"
        self.source.mkdir()
        self.state = self.project / ".runtime" / "expert-iteration"
        self.state.mkdir(parents=True)
        self.start = parse_timestamp("2026-11-12T13:00:00Z")
        self.config = Config(self.source, self.state, self.start, project_root=self.project)

    def result(self, **changes):
        result = {"window_start": utc_timestamp(self.start),
                  "window_end": utc_timestamp(self.start + PERIOD_SECONDS),
                  "status": "complete", "messages_examined": 3, "unresolved_examined": 2,
                  "confirmed_omissions": 1, "confirmed_misbindings": 0,
                  "summary": "sanitized test result"}
        return {**result, **changes}

    def test_timestamp_requires_explicit_timezone_and_normalizes_offset(self):
        self.assertEqual(self.start, parse_timestamp("2026-11-12T14:00:00+01:00"))
        with self.assertRaises(ValueError):
            parse_timestamp("2026-11-12T13:00:00")

    def test_next_window_is_not_dispatched_until_closed(self):
        progress = load_progress(self.config)
        self.assertIsNone(next_window(progress, self.start + PERIOD_SECONDS - 0.001))
        self.assertEqual((self.start, self.start + PERIOD_SECONDS),
                         next_window(progress, self.start + PERIOD_SECONDS))

    def test_restart_uses_contiguous_watermark_instead_of_current_time(self):
        atomic_json(self.state / "progress.json", {
            "first_start": self.start, "finished_watermark": self.start + PERIOD_SECONDS})
        progress = load_progress(self.config)
        self.assertEqual((self.start + PERIOD_SECONDS, self.start + PERIOD_SECONDS * 2),
                         next_window(progress, self.start + PERIOD_SECONDS * 10))

    def test_invalid_schedule_or_watermark_does_not_skip_windows(self):
        for progress in ({"first_start": self.start - PERIOD_SECONDS, "finished_watermark": self.start},
                         {"first_start": self.start, "finished_watermark": self.start + 1},
                         {"first_start": self.start, "finished_watermark": self.start - PERIOD_SECONDS}):
            with self.subTest(progress=progress):
                atomic_json(self.state / "progress.json", progress)
                with self.assertRaises(ValueError):
                    load_progress(self.config)

    def test_second_supervisor_cannot_take_same_lock(self):
        first = acquire_lock(self.state)
        try:
            with self.assertRaises(AlreadyRunning):
                acquire_lock(self.state)
        finally:
            first.close()
        second = acquire_lock(self.state)
        second.close()
        self.assertEqual(0o700, self.state.stat().st_mode & 0o777)

    def test_atomic_metadata_is_private(self):
        path = self.state / "test.json"
        atomic_json(path, {"state": "waiting"})
        self.assertEqual({"state": "waiting"}, json.loads(path.read_text()))
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_command_is_ephemeral_stdin_bounded_permission_without_resume(self):
        argv = command(self.config, self.state / "final.json")
        self.assertIn("--ephemeral", argv)
        self.assertIn("--json", argv)
        self.assertIn("--output-schema", argv)
        self.assertEqual("-", argv[-1])
        self.assertEqual("never", argv[argv.index("-a") + 1])
        self.assertEqual("danger-full-access", argv[argv.index("-s") + 1])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertNotIn("resume", argv)

    def test_prompt_sets_real_half_open_window_accuracy_and_transaction_boundaries(self):
        prompt = make_prompt(self.config, self.start, self.start + PERIOD_SECONDS, self.state)
        for term in ("2026-11-12T13:00:00Z", "2026-11-12T13:30:00Z", "半开窗口", "20秒",
                     "多专家", "不猜", "冻结", "Docker", "不执行实盘投注", "posted_at", "72小时",
                     "raw-evidence-manifest.json", "最早原始posted_at", "落盘前先写manifest"):
            self.assertIn(term, prompt)

    def test_zero_exit_without_complete_final_does_not_advance(self):
        def incomplete(config, prompt, directory):
            (directory / "final.json").write_text(json.dumps(self.result(status="blocked")))
            return 0, False
        record = run_window(self.config, self.start, self.start + PERIOD_SECONDS, runner=incomplete)
        self.assertEqual("failed", record["state"])
        self.assertEqual("agent_blocked", record["category"])
        progress = load_progress(self.config)
        self.assertFalse(commit_success(self.config, progress, record))
        self.assertEqual(self.start, progress["finished_watermark"])

    def test_failed_attempt_retained_and_same_window_retried(self):
        first = run_window(self.config, self.start, self.start + PERIOD_SECONDS,
                           runner=lambda *_: (9, False))
        second = run_window(self.config, self.start, self.start + PERIOD_SECONDS,
                            runner=lambda *_: (124, True))
        self.assertEqual(1, first["attempt"])
        self.assertEqual(2, second["attempt"])
        self.assertEqual("timed_out", second["state"])
        self.assertTrue(second["timed_out"])
        self.assertEqual("failed", second["previous_state"])
        self.assertEqual(9, json.loads((self.state / "history" / f"{self.start}-1.json").read_text())["exit_code"])
        self.assertEqual(self.start, load_progress(self.config)["finished_watermark"])

    def test_success_advances_exactly_one_window_after_result_validation(self):
        def success(config, prompt, directory):
            (directory / "final.json").write_text(json.dumps(self.result()))
            return 0, False
        record = run_window(self.config, self.start, self.start + PERIOD_SECONDS, runner=success)
        self.assertEqual("succeeded", record["state"])
        progress = load_progress(self.config)
        self.assertTrue(commit_success(self.config, progress, record))
        self.assertEqual(self.start + PERIOD_SECONDS, load_progress(self.config)["finished_watermark"])

    def test_crash_after_saved_success_does_not_repeat_child(self):
        atomic_json(self.state / "windows" / f"{self.start}.json", {
            "state": "succeeded", "exit_code": 0, "window_start": utc_timestamp(self.start),
            "window_end": utc_timestamp(self.start + PERIOD_SECONDS), "finished_at": utc_timestamp(self.start + 1900)})
        progress = load_progress(self.config)
        self.assertTrue(recover_recorded_success(self.config, progress))
        self.assertEqual(self.start + PERIOD_SECONDS, progress["finished_watermark"])
        self.assertFalse(recover_recorded_success(self.config, progress))

    def test_success_cannot_skip_or_duplicate_contiguous_window(self):
        progress = load_progress(self.config)
        bad = {"state": "succeeded", "exit_code": 0, "window_start": utc_timestamp(self.start + PERIOD_SECONDS),
               "window_end": utc_timestamp(self.start + PERIOD_SECONDS * 2), "finished_at": utc_timestamp(self.start)}
        with self.assertRaises(ValueError):
            commit_success(self.config, progress, bad)
        self.assertFalse((self.state / "progress.json").exists())

    def test_dry_run_never_calls_child_or_advances(self):
        def fail_if_called(*_):
            self.fail("Dry run must not invoke an agent")
        record = run_window(self.config, self.start, self.start + PERIOD_SECONDS,
                            runner=fail_if_called, dry_run=True)
        self.assertEqual("dry_run", record["state"])
        self.assertEqual(self.start, load_progress(self.config)["finished_watermark"])

    def test_bad_final_window_counts_and_unknown_status_are_rejected(self):
        final = self.state / "final.json"
        for changes, expected in (({"window_end": utc_timestamp(self.start)}, "result_window_mismatch"),
                                  ({"messages_examined": True}, "invalid_result_counts"),
                                  ({"confirmed_omissions": -1}, "invalid_result_counts"),
                                  ({"summary": ""}, "missing_result_summary"),
                                  ({"status": "sensitive arbitrary output"}, "agent_invalid_status")):
            with self.subTest(changes=changes):
                final.write_text(json.dumps(self.result(**changes)))
                self.assertEqual((False, expected), validate_result(final, self.start, self.start + PERIOD_SECONDS))

    def test_raw_pruning_uses_window_time_not_edit_time_and_preserves_metadata(self):
        raw = self.state / "attempts" / str(self.start) / "1"
        raw.mkdir(parents=True)
        for name in ("events.jsonl", "stderr.log", "final.json", "prompt.txt"):
            (raw / name).write_text("private test content")
        (raw / "agent-owned-file.txt").write_text("not owned by controller")
        meta = self.state / "windows" / f"{self.start}.json"
        atomic_json(meta, {"state": "failed"})
        self.assertEqual(0, prune_private_evidence(self.config, self.start + RAW_RETENTION_SECONDS - 1))
        self.assertEqual(4, prune_private_evidence(self.config, self.start + RAW_RETENTION_SECONDS))
        self.assertTrue(meta.exists())
        self.assertTrue((raw / "agent-owned-file.txt").exists())

    def test_environment_rejects_outside_project_and_non_half_hour_start(self):
        values = {"EXPERT_PROJECT_ROOT": str(self.project), "EXPERT_SOURCE_ROOT": str(self.source),
                  "EXPERT_ITERATION_STATE_DIR": str(self.state),
                  "EXPERT_ITERATION_FIRST_START": "2026-11-12T13:00:00Z"}
        with patch.dict(os.environ, values, clear=True):
            self.assertEqual(self.start, Config.from_environment().first_start)
            with patch.dict(os.environ, {"EXPERT_ITERATION_FIRST_START": "2026-11-12T13:00:01Z"}):
                with self.assertRaises(ValueError):
                    Config.from_environment()
            with patch.dict(os.environ, {"EXPERT_ITERATION_STATE_DIR": "/other/.runtime/private"}):
                with self.assertRaises(ValueError):
                    Config.from_environment()
            with patch.dict(os.environ, {"EXPERT_ITERATION_TIMEOUT_SECONDS": "1801"}):
                with self.assertRaises(ValueError):
                    Config.from_environment()

    def test_child_timeout_terminates_owned_process_group_and_reports_124(self):
        with patch("infra.monitor.expert_iteration_supervisor.subprocess.Popen") as launch, \
                patch("infra.monitor.expert_iteration_supervisor.capture_output") as capture, \
                patch("infra.monitor.expert_iteration_supervisor.os.killpg") as terminate:
            child = launch.return_value
            child.pid = 12345
            capture.side_effect = subprocess.TimeoutExpired("codex", 1790)
            child.wait.return_value = 0
            result = run_child(self.config, "test prompt", self.state)
            self.assertEqual((124, True), result)
            terminate.assert_called_once_with(12345, signal.SIGTERM)
            self.assertTrue(launch.call_args.kwargs["start_new_session"])
            self.assertEqual(1790, capture.call_args.args[3])
            self.assertEqual(subprocess.PIPE, launch.call_args.kwargs["stdout"])
            self.assertEqual(subprocess.PIPE, launch.call_args.kwargs["stderr"])

    def test_child_interrupt_cancels_owned_writer_before_releasing_lock(self):
        with patch("infra.monitor.expert_iteration_supervisor.subprocess.Popen") as launch, \
                patch("infra.monitor.expert_iteration_supervisor.capture_output") as capture, \
                patch("infra.monitor.expert_iteration_supervisor.os.killpg") as terminate:
            child = launch.return_value
            child.pid = 12345
            capture.side_effect = KeyboardInterrupt
            child.wait.return_value = 0
            with self.assertRaises(KeyboardInterrupt):
                run_child(self.config, "test prompt", self.state)
            terminate.assert_called_once_with(12345, signal.SIGTERM)

    def test_event_stream_persists_only_whitelisted_metadata_and_hashes(self):
        sink = RedactedOutput(self.state)
        payload = {"type": "item.completed", "item": {"id": "item_12", "type": "command_execution",
                   "status": "completed", "exit_code": 0, "command": "secret command",
                   "aggregated_output": "OLD REPLY ORIGINAL", "text": "personal-id"}}
        raw = (json.dumps(payload) + "\n").encode()
        sink.feed("stdout", raw[:17])
        sink.feed("stdout", raw[17:])
        sink.feed("stderr", b"private TG reply permission de")
        sink.feed("stderr", b"nied api-key")
        sink.close()
        event = json.loads((self.state / "events.jsonl").read_text())
        self.assertEqual({"type", "item_type", "id", "status", "exit_code", "bytes", "sha256"}, set(event))
        self.assertEqual(hashlib.sha256(raw).hexdigest(), event["sha256"])
        summary = json.loads((self.state / "output-summary.json").read_text())
        self.assertEqual(["permission_denied"], summary["stderr"]["failure_categories"])
        self.assertEqual(len(raw), summary["stdout"]["bytes"])
        persisted = "".join(path.read_text() for path in self.state.iterdir() if path.is_file())
        for sensitive in ("secret command", "OLD REPLY ORIGINAL", "personal-id", "private TG", "api-key"):
            self.assertNotIn(sensitive, persisted)
        self.assertFalse((self.state / "stderr.log").exists())

    def test_event_memory_is_bounded_and_untrusted_type_id_status_are_not_logged(self):
        sink = RedactedOutput(self.state)
        raw = b"x" * (MAX_EVENT_BYTES + 200)
        sink.feed("stdout", raw)
        self.assertLessEqual(len(sink.line), MAX_EVENT_BYTES)
        sink.feed("stdout", b"\n")
        sink.feed("stdout", b'{"type":["sensitive"],"id":"raw-message-id","status":{"secret":1}}\n')
        sink.close()
        records = [json.loads(line) for line in (self.state / "events.jsonl").read_text().splitlines()]
        self.assertEqual(["oversized_event", "unknown_event"], [item["type"] for item in records])
        self.assertNotIn("id", records[1])
        self.assertNotIn("status", records[1])

    def test_final_response_free_text_is_redacted_before_persisting(self):
        original = self.project / "temporary-final.json"
        original.write_text(json.dumps(self.result(summary="old raw TG ORIGINAL", evidence_paths=["personal-id"],
                                                    remaining_issues=["api-key"], latency_target_met=False)))
        final = self.state / "final.json"
        redact_final(original, final)
        self.assertEqual((True, "complete"), validate_result(final, self.start, self.start + PERIOD_SECONDS))
        self.assertNotIn("ORIGINAL", final.read_text())
        self.assertNotIn("personal-id", final.read_text())
        self.assertNotIn("api-key", final.read_text())
        self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), json.loads(final.read_text())["sha256"])

    def test_old_message_export_expires_by_posted_at_inside_a_new_window(self):
        attempt = self.state / "attempts" / str(self.start) / "1"
        (attempt / "raw").mkdir(parents=True)
        (attempt / "raw" / "expired.json").write_text("old reply")
        (attempt / "raw" / "fresh.json").write_text("current reply")
        atomic_json(attempt / "raw-evidence-manifest.json", {"schema_version": 1, "files": [
            {"path": "raw/expired.json", "posted_at": utc_timestamp(self.start - RAW_RETENTION_SECONDS)},
            {"path": "raw/fresh.json", "posted_at": utc_timestamp(self.start - 10)}]})
        self.assertEqual(1, prune_private_evidence(self.config, self.start))
        self.assertFalse((attempt / "raw" / "expired.json").exists())
        self.assertTrue((attempt / "raw" / "fresh.json").exists())
        self.assertEqual(1, len(json.loads((attempt / "raw-evidence-manifest.json").read_text())["files"]))

    def test_invalid_manifest_cannot_delete_outside_attempt_and_blocks_success(self):
        outside = self.project / "preserved.txt"
        outside.write_text("do not delete")
        def success_with_bad_manifest(config, prompt, directory):
            (directory / "final.json").write_text(json.dumps(self.result()))
            atomic_json(directory / "raw-evidence-manifest.json", {"schema_version": 1, "files": [
                {"path": "../preserved.txt", "posted_at": utc_timestamp(self.start - RAW_RETENTION_SECONDS)}]})
            return 0, False
        record = run_window(self.config, self.start, self.start + PERIOD_SECONDS,
                            clock=lambda: self.start, runner=success_with_bad_manifest)
        self.assertEqual("failed", record["state"])
        self.assertEqual("invalid_raw_evidence_manifest", record["category"])
        self.assertTrue(outside.exists())

    def test_real_pipe_capture_redacts_tool_output_without_model_or_disk_stdout(self):
        code = ("import json,sys;sys.stdin.buffer.read();"
                "print(json.dumps({'type':'item.completed','item':{'id':'item_1','type':'command_execution',"
                "'status':'completed','exit_code':0,'aggregated_output':'old reply content'}}));"
                "sys.stderr.write('private warning');")
        with patch("infra.monitor.expert_iteration_supervisor.command", return_value=[sys.executable, "-c", code]):
            self.assertEqual((0, False), run_child(self.config, "test stdin", self.state))
        persisted = "".join(path.read_text() for path in self.state.iterdir() if path.is_file())
        self.assertNotIn("old reply content", persisted)
        self.assertNotIn("private warning", persisted)
        summary = json.loads((self.state / "output-summary.json").read_text())
        self.assertEqual(1, summary["events"])


if __name__ == "__main__":
    unittest.main()
