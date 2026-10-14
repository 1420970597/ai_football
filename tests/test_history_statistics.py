"""Regression coverage for the asynchronous history statistics contract.

These tests use fake readers and projection rows only.  They never connect to
the configured database and never submit a betting request.
"""
from __future__ import annotations

import json
import copy
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from service.ledger import DecisionLedger, LedgerEntry
from store.ledger_db import FIELDS


class _QueryCancelled(RuntimeError):
    sqlstate = "57014"


class _OtherQueryFailure(RuntimeError):
    sqlstate = "XX000"


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self):
        return self.now


class _DiagnosticConnection:
    """Run real fixture SQL while emulating only PG budgets/cancellation."""

    def __init__(self, inner, *, failure=None, clock=None,
                 identity_elapsed=0.0, payload_elapsed=0.0,
                 empty_identity_page=None):
        self.inner = inner
        self.failure = failure
        self.clock = clock
        self.identity_elapsed = identity_elapsed
        self.payload_elapsed = payload_elapsed
        self.empty_identity_page = empty_identity_page
        self.queries: list[str] = []
        self.timeouts: list[int] = []
        self.identity_pages = 0
        self.payload_pages = 0
        self.cancelled = False
        self.after_cancellation = 0

    def execute(self, sql, params=()):
        self.queries.append(sql)
        if self.cancelled:
            self.after_cancellation += 1
            raise AssertionError("SQL issued after cancellation")
        if sql.startswith("SELECT set_config('statement_timeout'"):
            self.timeouts.append(int(params[0]))
            return MagicMock()
        stage = None
        page = 0
        elapsed = 0.0
        if sql.startswith("SELECT identity FROM"):
            stage = "identities"
            self.identity_pages += 1
            page = self.identity_pages
            elapsed = self.identity_elapsed
        elif sql.startswith("SELECT identity,payload"):
            stage = "payloads"
            self.payload_pages += 1
            page = self.payload_pages
            elapsed = self.payload_elapsed
        if self.failure is not None and self.failure[:2] == (stage, page):
            self.cancelled = True
            raise self.failure[2]
        if self.clock is not None:
            self.clock.now += elapsed
        if stage == "identities" and page == self.empty_identity_page:
            return []
        return self.inner.execute(sql, params)


class _Reader:
    backend = "postgres"

    def __init__(self) -> None:
        self.connection = MagicMock()
        self.connection.execute.return_value.close.return_value = None


class _DB:
    backend = "postgres"

    def __init__(self, reader: _Reader) -> None:
        self.reader_connection = reader
        self.reader_calls = 0
        self.events: list[str] = []

    def reader(self):
        self.reader_calls += 1
        return _ReaderTransaction(self, self.reader_calls)


class _ReaderTransaction:
    def __init__(self, database: _DB, number: int) -> None:
        self.database = database
        self.number = number

    def __enter__(self):
        self.database.events.append(f"reader-{self.number}-begin")
        return self.database.reader_connection if self.number == 1 else _Reader()

    def __exit__(self, exception_type, _exception, _traceback):
        state = "rollback" if exception_type else "commit"
        self.database.events.append(f"reader-{self.number}-{state}")
        return False


class _CacheWriter:
    def __init__(self, ledger: DecisionLedger, events: list[str]) -> None:
        self.ledger = ledger
        self.events = events
        self.saved: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *_arguments):
        self.events.append("cache-commit")
        return False

    def execute(self, sql, params=()):
        if sql.startswith("INSERT INTO ledger_history_cache"):
            assert params[0] in self.ledger._history_cache, "cache must publish memory first"
            assert self.ledger._history_cache[params[0]]["statistics_status"] == "ready"
            self.saved.append(json.loads(params[1]))
            self.events.append("cache-insert")
        return MagicMock()

    def close(self):
        self.events.append("cache-close")


def _core_snapshot() -> dict:
    return {
        "overall": {"total": 7, "profit_units": 1.25},
        "settled": {"graded": 5},
        "timeline": [{"date": "2026-10-14", "n": 5}],
        "portfolio_summary": {"total": 5},
        "statistics_status": "ready",
        "statistics_at": "2026-10-14T10:00:00+00:00",
        "statistics_timings": {"aggregate_many": {"elapsed_ms": 2}},
    }


class HistoryStatisticsTests(unittest.TestCase):
    def setUp(self):
        # Production PG configuration must never redirect these unit fixtures.
        storage = patch("store.ledger_db.postgres_enabled", return_value=False)
        storage.start()
        self.addCleanup(storage.stop)

    def _queue_job(self, ledger: DecisionLedger, db: _DB) -> None:
        ledger._history_jobs["cache-key"] = (
            db, "1=1", (), "1=1", (), "real", 0,
        )

    def test_experiment_timeout_persists_core_first_and_keeps_old_experiment(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            ledger = DecisionLedger(root)
            reader = _Reader()
            db = _DB(reader)
            self._queue_job(ledger, db)
            old = {"arms": {"economics_llm": {"total": 3}}, "errors": {"old": 1}}
            cached = {"_refresh_statistics": True, "experiments": old,
                      "experiments_at": "2026-10-13T10:00:00+00:00"}
            writer = _CacheWriter(ledger, db.events)

            def cancel_experiment(experiment_reader, *_arguments, **_keywords):
                self.assertIsNot(experiment_reader, reader)
                self.assertEqual(len(writer.saved), 1)
                self.assertIn("reader-1-commit", db.events)
                return self._cancelled()

            with patch.object(ledger, "_cached_history_snapshot", return_value=cached), \
                    patch.object(ledger, "_history_snapshot", return_value=_core_snapshot()) as core, \
                    patch("store.postgres.connect", return_value=writer), \
                    patch.object(ledger, "_experiment_stats", side_effect=cancel_experiment):
                ledger._history_statistics_loop()
            saves = writer.saved
            self.assertIs(core.call_args.kwargs["include_experiments"], False)
            self.assertEqual(len(saves), 2)
            self.assertEqual(saves[0]["statistics_status"], "ready")
            self.assertEqual(saves[0]["experiments"], old)
            self.assertEqual(saves[1]["experiments"], old)
            self.assertEqual(saves[1]["experiments_at"], "2026-10-13T10:00:00+00:00")
            self.assertEqual(saves[1]["experiments_status"], "error")
            self.assertEqual(saves[1]["experiments_error"]["stage"], "experiments")
            self.assertEqual(saves[1]["experiments_error"]["sqlstate"], "57014")
            self.assertNotIn("secret", json.dumps(saves[1]["experiments_error"]))
            self.assertEqual(set(saves[1]["experiments_error"]),
                             {"stage", "exception_type", "sqlstate", "elapsed_ms"})
            self.assertEqual(db.reader_calls, 2)
            self.assertLess(db.events.index("reader-1-commit"), db.events.index("cache-insert"))
            self.assertLess(db.events.index("cache-commit"), db.events.index("reader-2-begin"))
            self.assertIn("reader-2-rollback", db.events)
            self.assertEqual(ledger._history_cache["cache-key"]["statistics_status"], "ready")
            self.assertEqual(ledger._history_cache["cache-key"]["experiments"], old)

    @staticmethod
    def _cancelled(*_args, **_kwargs):
        error = _QueryCancelled("secret connection details")
        raise error

    def test_successful_empty_experiment_result_overwrites_old_snapshot(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            ledger = DecisionLedger(root)
            db = _DB(_Reader())
            self._queue_job(ledger, db)
            cached = {"_refresh_statistics": True, "experiments": {"old": 9},
                      "experiments_at": "2026-10-13T00:00:00+00:00"}
            saves: list[dict] = []
            with patch.object(ledger, "_cached_history_snapshot", return_value=cached), \
                    patch.object(ledger, "_history_snapshot", return_value=_core_snapshot()), \
                    patch.object(ledger, "_save_history_snapshot",
                                 side_effect=lambda _key, value: saves.append(copy.deepcopy(value))), \
                    patch.object(ledger, "_experiment_stats", return_value={}):
                ledger._history_statistics_loop()
            self.assertEqual(saves[-1]["experiments"], {})
            self.assertEqual(saves[-1]["experiments_status"], "ready")
            self.assertTrue(saves[-1]["experiments_at"])
            self.assertNotIn("experiments_error", saves[-1])

    def test_cache_write_failure_keeps_successful_core_in_memory(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            ledger = DecisionLedger(root)
            snapshot = _core_snapshot()
            writer = _CacheWriter(ledger, [])
            original_execute = writer.execute
            def cancelled_write(sql, params=()):
                result = original_execute(sql, params)
                if sql.startswith("INSERT INTO ledger_history_cache"):
                    raise OSError("secret password")
                return result
            with patch("store.postgres.connect", return_value=writer), \
                    patch.object(writer, "execute", side_effect=cancelled_write), \
                    self.assertLogs("service.ledger", level="WARNING") as captured:
                detail = ledger._save_history_snapshot("cache-key", snapshot)
            assert detail is not None
            self.assertEqual(detail["stage"], "cache_write")
            self.assertEqual(detail["exception_type"], "OSError")
            self.assertNotIn("password", json.dumps(detail))
            self.assertNotIn("password", str(captured.output))
            self.assertEqual(len(writer.saved), 1)
            self.assertEqual(ledger._history_cache["cache-key"]["overall"], snapshot["overall"])
            self.assertEqual(ledger._history_cache["cache-key"]["statistics_persist_error"], detail)

    def test_history_cold_get_keeps_unknown_statistics_instead_of_zero(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            ledger = DecisionLedger(root)
            database = ledger._db
            assert database is not None
            original = database.connection
            database.backend = "postgres"
            database.connection = MagicMock()
            database.connection.execute.side_effect = AssertionError("foreground database read")
            try:
                with patch("service.ledger.threading.Thread") as thread:
                    result = ledger.history(limit=20, offset=0, competition_type="real")
                self.assertIsNone(result["overall"])
                self.assertIsNone(result["settled"])
                self.assertEqual(result["statistics_status"], "loading")
                self.assertIsNone(result["statistics_at"])
                thread.assert_called_once()
            finally:
                database.backend = "sqlite"
                database.connection = original

    def test_old_decisions_and_statistics_projection_have_same_experiment_contract(self):
        ledger, database = self._experiment_fixture()
        queries: list[str] = []
        database.connection.set_trace_callback(queries.append)
        with patch.object(database, "_statistics_table", return_value="decisions"):
            actual = ledger._experiment_stats(database, "real", 1)
        queries.clear()
        with patch.object(database, "_statistics_table", return_value="decision_statistics"):
            projected = ledger._experiment_stats(database, "real", 1)
        actual.pop("timings")
        projected.pop("timings")
        self.assertEqual(actual, projected)
        self.assertEqual(projected["review_status"], {"ready": 2, "error": 3})
        self.assertEqual(projected["errors"], {"timeout": 2, "unavailable": 1})
        self.assertEqual(projected["errors_status"], "complete")
        self.assertEqual(projected["errors_counted"], 3)
        self.assertEqual(projected["errors_total"], 3)
        self.assertEqual(projected["paired_opportunities"], 3)
        self.assertAlmostEqual(projected["coverage"], 2 / 3)
        self.assertEqual(projected["profit_difference"], -.5)
        self.assertEqual(projected["operation"]["economics_llm"]["total"], 3)
        self.assertEqual(projected["paired"]["economics_llm"]["total"], 2)
        self.assertTrue(any("FROM decision_statistics WHERE" in sql
                            and "GROUP BY review_status" in sql for sql in queries))
        paired_queries = [sql for sql in queries if "experiment_id IN (SELECT" in sql]
        self.assertTrue(paired_queries)
        self.assertTrue(all("SELECT experiment_id FROM decision_statistics" in sql
                            for sql in paired_queries))
        self.assertFalse(any("json_extract(payload" in sql for sql in queries))
        payload_queries = [sql for sql in queries if "SELECT identity,payload" in sql]
        self.assertTrue(payload_queries)
        self.assertTrue(all("FROM decisions WHERE identity IN (" in sql
                            for sql in payload_queries))

    def test_projection_error_scan_reports_partial_lower_bound_at_cap(self):
        ledger, database = self._experiment_fixture()
        queries: list[str] = []
        database.connection.set_trace_callback(queries.append)
        for limit, expected_status in [(2, "partial"), (3, "complete"), (10, "complete")]:
            results = []
            for table in ("decisions", "decision_statistics"):
                with self.subTest(limit=limit, table=table), \
                        patch.object(database, "_statistics_table", return_value=table), \
                        patch("service.ledger.EXPERIMENT_ERROR_LIMIT", limit), \
                        patch("service.ledger.EXPERIMENT_ERROR_BATCH", 2):
                    result = ledger._experiment_stats(database, "real", 1)
                self.assertEqual(result["errors_status"], expected_status)
                self.assertEqual(result["errors_counted"], min(limit, 3))
                self.assertEqual(result["errors_total"], 3)
                self.assertEqual(result["errors_limit"], limit)
                self.assertEqual(sum(result["errors"].values()), min(limit, 3))
                self.assertEqual(result["review_status"]["error"], 3)
                self.assertTrue(all(count <= {"timeout": 2, "unavailable": 1}[code]
                                    for code, count in result["errors"].items()))
                result.pop("timings")
                results.append(result)
            self.assertEqual(results[0], results[1])
        identity_queries = [sql for sql in queries if sql.startswith("SELECT identity FROM")]
        self.assertTrue(any("AND identity>" in sql for sql in identity_queries))
        self.assertTrue(all(sql.endswith("LIMIT 2") for sql in identity_queries))

    def test_projection_error_payloads_can_be_invalid_or_nondictionary_json(self):
        ledger, database = self._experiment_fixture()
        identities = [row[0] for row in database.connection.execute(
            "SELECT identity FROM decisions WHERE review_status='error' "
            "AND competition_type='real' ORDER BY identity LIMIT 3")]
        for payloads in (("[]", "null", "broken json"), ('"text"', "false", "9")):
            for identity, payload in zip(identities, payloads):
                database.connection.execute("UPDATE decisions SET payload=? WHERE identity=?",
                                            (payload, identity))
            with self.subTest(payloads=payloads), \
                    patch.object(database, "_statistics_table", return_value="decision_statistics"):
                result = ledger._experiment_stats(database, "real", 1)
            self.assertEqual(result["errors"], {})
            self.assertEqual(result["errors_status"], "partial")
            self.assertEqual(result["errors_counted"], 0)
            self.assertEqual(result["errors_total"], 3)

    def test_missing_payload_retains_projection_total_and_main_metrics(self):
        ledger, database = self._experiment_fixture()
        with patch.object(database, "_statistics_table", return_value="decision_statistics"):
            expected = ledger._experiment_stats(database, "real", 1)
        identity = self._error_identities(database)[0]
        database.connection.execute("DELETE FROM decisions WHERE identity=?", (identity,))
        with patch.object(database, "_statistics_table", return_value="decision_statistics"):
            actual = ledger._experiment_stats(database, "real", 1)
        self._assert_main_metrics(actual, expected)
        self.assertEqual(actual["errors_status"], "partial")
        self.assertEqual(actual["errors_counted"], 2)
        self.assertEqual(actual["errors_total"], 3)
        self.assertEqual(sum(actual["errors"].values()), 2)

    def test_only_decoded_dictionaries_count_and_absent_error_code_is_unknown(self):
        ledger, database = self._experiment_fixture()
        for identity, payload in zip(self._error_identities(database), ("{}", "[]", "broken json")):
            database.connection.execute("UPDATE decisions SET payload=? WHERE identity=?",
                                        (payload, identity))
        with patch.object(database, "_statistics_table", return_value="decision_statistics"):
            result = ledger._experiment_stats(database, "real", 1)
        self.assertEqual(result["errors"], {"unknown": 1})
        self.assertEqual(result["errors_status"], "partial")
        self.assertEqual(result["errors_counted"], 1)
        self.assertEqual(result["errors_total"], 3)

    def test_early_identity_exhaustion_cannot_claim_complete(self):
        ledger, database = self._experiment_fixture()
        with patch.object(database, "_statistics_table", return_value="decision_statistics"):
            expected = ledger._experiment_stats(database, "real", 1)
        for page, counted in ((1, 0), (2, 2)):
            proxy = _DiagnosticConnection(database.connection, empty_identity_page=page)
            with self.subTest(page=page), \
                    patch.object(database, "connection", proxy), \
                    patch.object(database, "_statistics_table", return_value="decision_statistics"), \
                    patch("service.ledger.EXPERIMENT_ERROR_BATCH", 2):
                actual = ledger._experiment_stats(database, "real", 1)
            self._assert_main_metrics(actual, expected)
            self.assertEqual(actual["errors_status"], "partial")
            self.assertEqual(actual["errors_counted"], counted)
            self.assertEqual(actual["errors_total"], 3)
            self.assertEqual(sum(actual["errors"].values()), counted)

    def test_identity_timeout_preserves_main_metrics_without_further_sql(self):
        ledger, database = self._experiment_fixture()
        with patch.object(database, "_statistics_table", return_value="decision_statistics"):
            expected = ledger._experiment_stats(database, "real", 1)
        proxy = _DiagnosticConnection(database.connection,
            failure=("identities", 1, TimeoutError("secret details")))
        with patch.object(database, "backend", "postgres"), \
                patch.object(database, "connection", proxy), \
                patch.object(database, "_statistics_table", return_value="decision_statistics"), \
                self.assertLogs("service.ledger", level="WARNING") as captured:
            actual = ledger._experiment_stats(database, "real", 1,
                                              deadline=time.monotonic() + 30)
        self._assert_main_metrics(actual, expected)
        self._assert_diagnostic(actual, "experiment_error_identities", "TimeoutError", "", 0)
        self.assertEqual(proxy.payload_pages, 0)
        self.assertEqual(proxy.after_cancellation, 0)
        self.assertTrue(proxy.queries[-1].startswith("SELECT identity FROM"))
        self.assertNotIn("secret", str(captured.output))

    def test_payload_cancellation_retains_successful_page_and_main_metrics(self):
        ledger, database = self._experiment_fixture()
        with patch.object(database, "_statistics_table", return_value="decision_statistics"):
            expected = ledger._experiment_stats(database, "real", 1)
        for failure_page, counted in ((1, 0), (2, 2)):
            proxy = _DiagnosticConnection(database.connection,
                failure=("payloads", failure_page, _QueryCancelled("secret details")))
            with self.subTest(failure_page=failure_page), \
                    patch.object(database, "backend", "postgres"), \
                    patch.object(database, "connection", proxy), \
                    patch.object(database, "_statistics_table", return_value="decision_statistics"), \
                    patch("service.ledger.EXPERIMENT_ERROR_BATCH", 2), \
                    self.assertLogs("service.ledger", level="WARNING") as captured:
                actual = ledger._experiment_stats(database, "real", 1,
                                                  deadline=time.monotonic() + 30)
            self._assert_main_metrics(actual, expected)
            self._assert_diagnostic(actual, "experiment_error_payloads", "_QueryCancelled", "57014", counted)
            self.assertEqual(proxy.after_cancellation, 0)
            self.assertTrue(proxy.queries[-1].startswith("SELECT identity,payload"))
            self.assertEqual(proxy.identity_pages, failure_page)
            self.assertEqual(proxy.payload_pages, failure_page)
            self.assertNotIn("secret", str(captured.output))
            if counted:
                first_codes = [json.loads(row[0])["review_error"] for row in
                    database.connection.execute("SELECT payload FROM decisions WHERE identity IN (?,?)",
                                                self._error_identities(database)[:2])]
                self.assertEqual(actual["errors"], {code: first_codes.count(code) for code in set(first_codes)})

    def test_local_diagnostic_budget_decreases_for_each_identity_and_payload_page(self):
        ledger, database = self._experiment_fixture()
        clock = _Clock()
        proxy = _DiagnosticConnection(database.connection, clock=clock,
                                      identity_elapsed=1.5, payload_elapsed=.5)
        with patch.object(database, "backend", "postgres"), \
                patch.object(database, "connection", proxy), \
                patch.object(database, "_statistics_table", return_value="decision_statistics"), \
                patch("service.ledger.EXPERIMENT_ERROR_BATCH", 2), \
                patch("service.ledger.EXPERIMENT_ERROR_DEADLINE_S", 5.), \
                patch("service.ledger.time.monotonic", clock):
            actual = ledger._experiment_stats(database, "real", 1, deadline=130.)
        self.assertEqual(proxy.timeouts, [30000] * 4 + [5000, 3500, 3000, 1500])
        self.assertEqual(actual["errors_status"], "complete")
        self.assertEqual(actual["errors_counted"], 3)
        self.assertEqual(actual["errors_total"], 3)
        self.assertNotIn("diagnostic_error", actual)

    def test_global_deadline_exhaustion_preserves_previous_page_without_more_sql(self):
        ledger, database = self._experiment_fixture()
        with patch.object(database, "_statistics_table", return_value="decision_statistics"):
            expected = ledger._experiment_stats(database, "real", 1)
        clock = _Clock()
        proxy = _DiagnosticConnection(database.connection, clock=clock,
                                      identity_elapsed=1.5, payload_elapsed=.5)
        with patch.object(database, "backend", "postgres"), \
                patch.object(database, "connection", proxy), \
                patch.object(database, "_statistics_table", return_value="decision_statistics"), \
                patch("service.ledger.EXPERIMENT_ERROR_BATCH", 2), \
                patch("service.ledger.EXPERIMENT_ERROR_DEADLINE_S", 5.), \
                patch("service.ledger.time.monotonic", clock), \
                self.assertLogs("service.ledger", level="WARNING"):
            actual = ledger._experiment_stats(database, "real", 1, deadline=102.)
        self._assert_main_metrics(actual, expected)
        self._assert_diagnostic(actual, "experiment_error_identities", "TimeoutError", "", 2)
        self.assertEqual(proxy.timeouts, [2000] * 4 + [2000, 500])
        self.assertEqual(proxy.identity_pages, 1)
        self.assertEqual(proxy.payload_pages, 1)
        self.assertTrue(proxy.queries[-1].startswith("SELECT identity,payload"))

    def test_unexpected_diagnostic_errors_propagate_instead_of_partial_success(self):
        ledger, database = self._experiment_fixture()
        for error in (RuntimeError("bug"), _OtherQueryFailure("server bug")):
            for stage in ("identities", "payloads"):
                proxy = _DiagnosticConnection(database.connection, failure=(stage, 1, error))
                with self.subTest(error=type(error).__name__, stage=stage), \
                        patch.object(database, "backend", "postgres"), \
                        patch.object(database, "connection", proxy), \
                        patch.object(database, "_statistics_table", return_value="decision_statistics"), \
                        self.assertRaises(type(error)) as captured:
                    ledger._experiment_stats(database, "real", 1,
                                             deadline=time.monotonic() + 30)
                self.assertIs(captured.exception, error)
                self.assertEqual(proxy.after_cancellation, 0)

    def _assert_main_metrics(self, actual, expected):
        for name in ("arms", "paired", "operation", "review_status", "paired_opportunities",
                     "coverage", "profit_difference", "accuracy_difference", "conclusion"):
            self.assertEqual(actual[name], expected[name], name)

    def _assert_diagnostic(self, result, stage, exception_type, sqlstate, counted):
        self.assertEqual(result["errors_status"], "partial")
        self.assertEqual(result["errors_total"], 3)
        self.assertEqual(result["errors_counted"], counted)
        self.assertEqual(sum(result["errors"].values()), counted)
        detail = result["diagnostic_error"]
        self.assertEqual(detail["stage"], stage)
        self.assertEqual(detail["exception_type"], exception_type)
        self.assertEqual(detail["sqlstate"], sqlstate)
        self.assertGreaterEqual(detail["elapsed_ms"], 0)
        self.assertEqual(set(detail), {"stage", "exception_type", "sqlstate", "elapsed_ms"})
        self.assertNotIn("secret", json.dumps(detail))

    @staticmethod
    def _error_identities(database):
        return [row[0] for row in database.connection.execute(
            "SELECT identity FROM decision_statistics WHERE review_status='error' "
            "AND competition_type='real' ORDER BY identity")]

    def test_phase_error_records_substage_without_logging_exception_message(self):
        ledger = DecisionLedger()
        timings: dict = {}
        started = time.monotonic()
        error = _QueryCancelled("secret connection or query details")
        with self.assertLogs("service.ledger", level="WARNING") as captured:
            try:
                with ledger._history_phase("experiment_paired", timings):
                    raise error
            except RuntimeError as caught:
                detail = ledger._history_failure("experiments", caught, started)
        self.assertEqual(detail["stage"], "experiment_paired")
        self.assertEqual(detail["sqlstate"], "57014")
        self.assertEqual(detail["exception_type"], "_QueryCancelled")
        self.assertGreaterEqual(detail["elapsed_ms"], 0)
        self.assertNotIn("secret", str(captured.output))
        self.assertIn("elapsed_ms", timings["experiment_paired"])

    def test_core_snapshot_can_finish_without_running_experiment_queries(self):
        ledger, database = self._experiment_fixture()
        with patch.object(ledger, "_experiment_stats", side_effect=AssertionError("experiment queried")):
            snapshot = ledger._history_snapshot(database, "1=1", (), "1=1", (),
                                                "real", 0, include_experiments=False)
        self.assertEqual(snapshot["statistics_status"], "ready")
        self.assertEqual(snapshot["overall"]["total"], 13)
        self.assertNotIn("experiments", snapshot)
        self.assertNotIn("experiments_at", snapshot)

    def _experiment_fixture(self):
        temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(temporary.cleanup)
        ledger = DecisionLedger(temporary.name)
        database = ledger._db
        assert database is not None
        self.addCleanup(database.connection.close)
        clock = patch("service.ledger._now", return_value=datetime(2026, 10, 14, 12, tzinfo=timezone.utc))
        clock.start()
        self.addCleanup(clock.stop)
        rows = []

        def add(experiment, algorithm, status, profit, review="ready", error="",
                kind="real", at="2026-10-14T10:00:00+00:00"):
            rows.append(LedgerEntry(at=at, match_id=experiment,
                decision_id=experiment + "-" + algorithm, experiment_id=experiment,
                algorithm=algorithm, market="OU", line="2.5", outcome="over",
                competition_type=kind, status=status, pnl=profit, is_pick=True, odds=2.,
                review_status=review, review_error=error).as_dict())

        for experiment, control, learned in [
                ("paired-win", ("won", 1.), ("lost", -1.)),
                ("paired-push", ("push", 0.), ("half_won", .5))]:
            add(experiment, "economics_control", *control)
            add(experiment, "economics_llm", *learned)
        add("error-paired", "economics_control", "lost", -1.)
        add("error-paired", "economics_llm", "lost", -1., "error", "timeout")
        add("pending", "economics_control", "won", 1.)
        add("pending", "economics_llm", "pending", 0., "error", "unavailable")
        add("unpaired", "economics_llm", "lost", -1., "error", "timeout")
        for algorithm in ("economics_control", "economics_llm"):
            add("virtual", algorithm, "won", 1., kind="virtual")
            add("old", algorithm, "won", 1., at="2026-09-01T10:00:00+00:00")
        database.put(rows, lambda stamp: datetime.fromisoformat(stamp).timestamp())
        names = ",".join(["identity", *FIELDS])
        database.connection.execute("CREATE TABLE decision_statistics AS SELECT " +
                                    names + " FROM decisions")
        database.connection.commit()
        return ledger, database


if __name__ == "__main__":
    unittest.main()
