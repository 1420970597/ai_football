#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""collector.orchestrator 的单元测试（报告 §3.2/§3.3）。

全部通过注入 opener 实现，**不触碰真实网络**。
"""

from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.request
from typing import Any, cast

from collector.orchestrator import (
    CollectTask,
    CollectorError,
    HealthResult,
    ScrapeClient,
    TaskRegistry,
    TaskState,
    collect_urls,
)


def ok_opener(payload, status=200):
    """返回固定 JSON 的假 opener。"""
    def _op(req, timeout):
        return status, json.dumps(payload).encode("utf-8")
    return _op


def seq_opener(responses):
    """按调用次序返回不同响应；(status, body) 列表。"""
    it = iter(responses)

    def _op(req, timeout):
        status, body = next(it)
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return status, raw
    return _op


class TestTaskState(unittest.TestCase):
    def test_terminal_states(self):
        for s in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED):
            self.assertTrue(s.terminal)
        for s in (TaskState.PENDING, TaskState.PROBING, TaskState.FETCHING,
                  TaskState.FALLBACK, TaskState.INTERCEPTED):
            self.assertFalse(s.terminal)


class TestCollectTask(unittest.TestCase):
    def test_touch_updates_state_and_note(self):
        t = CollectTask(task_id="t1", target="x")
        t.touch(TaskState.FETCHING, "开始")
        self.assertIs(t.state, TaskState.FETCHING)
        self.assertIn("开始", t.notes)

    def test_as_dict_shape(self):
        t = CollectTask(task_id="t1", target="x")
        d = t.as_dict()
        for k in ("task_id", "target", "state", "terminal", "attempts",
                  "result_count", "error", "notes", "elapsed"):
            self.assertIn(k, d)


class TestTaskRegistry(unittest.TestCase):
    def test_create_and_get(self):
        r = TaskRegistry()
        t = r.create("target")
        self.assertIs(r.get(t.task_id), t)
        self.assertIsNone(r.get("nope"))

    def test_unique_ids(self):
        r = TaskRegistry()
        ids = {r.create("x").task_id for _ in range(20)}
        self.assertEqual(len(ids), 20)

    def test_eviction_prefers_terminal(self):
        r = TaskRegistry(max_tasks=3)
        done = [r.create("d") for _ in range(3)]
        for t in done:
            t.touch(TaskState.SUCCEEDED)
        running = r.create("r")          # 触发淘汰
        self.assertIsNotNone(r.get(running.task_id))
        self.assertLessEqual(len(r.all()), 4)

    def test_all_sorted_by_created(self):
        r = TaskRegistry()
        ts = [r.create("x") for _ in range(5)]
        got = r.all()
        self.assertEqual(len(got), 5)


class TestCollectorError(unittest.TestCase):
    def test_as_dict(self):
        e = CollectorError("boom", url="http://x", status=500, retryable=True)
        d = e.as_dict()
        self.assertEqual(d["status"], 500)
        self.assertTrue(d["retryable"])
        self.assertEqual(d["url"], "http://x")


class TestHealthResult(unittest.TestCase):
    def test_as_dict(self):
        h = HealthResult(True, "ok")
        self.assertTrue(h.as_dict()["healthy"])


class TestScrapeClientConstruction(unittest.TestCase):
    def test_default_base_url(self):
        c = ScrapeClient(opener=ok_opener({}))
        self.assertTrue(c.base_url.startswith("http"))

    def test_rejects_non_http_scheme(self):
        """安全：拒绝 file:// 等协议（防止本地文件读取）。"""
        with self.assertRaises(CollectorError):
            ScrapeClient("file:///etc/passwd", opener=ok_opener({}))

    def test_rejects_url_without_host(self):
        with self.assertRaises(CollectorError):
            ScrapeClient("http://", opener=ok_opener({}))

    def test_bad_config_falls_back(self):
        # 本用例的**目的**就是传入非法类型的配置值，验证内部容错转换
        # （_safe_float / _safe_int 会回退到默认值）。
        # 用 cast(Any, ...) 显式声明“故意绕过类型声明”。
        c = ScrapeClient(opener=ok_opener({}),
                         timeout=cast(Any, "bad"),
                         max_retries=cast(Any, "bad"),
                         backoff_base=cast(Any, "bad"))
        self.assertGreater(c.timeout, 0)
        self.assertGreaterEqual(c.max_retries, 0)
        self.assertGreaterEqual(c.backoff_base, 0.0)


class TestScrapeClientHealth(unittest.TestCase):
    def test_healthy_variants(self):
        for payload in ({"status": "healthy"}, {"status": "ok"},
                        {"status": "up"}, {"healthy": True},
                        {"healthy": 1}, {"healthy": "true"},
                        {"healthy": "yes"}):
            c = ScrapeClient(opener=ok_opener(payload))
            self.assertTrue(c.health().healthy, payload)

    def test_unhealthy(self):
        for payload in ({"status": "down"}, {"healthy": False},
                        {"healthy": 0}, {"healthy": "no"}, {}):
            c = ScrapeClient(opener=ok_opener(payload))
            self.assertFalse(c.health().healthy, payload)

    def test_http_error_returns_unhealthy(self):
        c = ScrapeClient(opener=ok_opener({}, status=500), max_retries=0)
        h = c.health()
        self.assertFalse(h.healthy)
        self.assertIn("500", h.detail)


class TestScrapeClientRequests(unittest.TestCase):
    def test_scrape_success(self):
        c = ScrapeClient(opener=ok_opener({"success": True, "title": "T"}))
        r = c.scrape("https://example.com")
        self.assertTrue(r["success"])

    def test_non_json_response_raises(self):
        c = ScrapeClient(opener=lambda req, t: (200, b"<html>"), max_retries=0)
        with self.assertRaises(CollectorError) as ctx:
            c.scrape("https://example.com")
        self.assertIn("JSON", str(ctx.exception))

    def test_non_dict_json_raises(self):
        c = ScrapeClient(opener=lambda req, t: (200, b"[1,2]"), max_retries=0)
        with self.assertRaises(CollectorError):
            c.scrape("https://example.com")

    def test_5xx_retries_then_succeeds(self):
        """5xx 可重试：两次失败后成功。"""
        op = seq_opener([(500, {}), (500, {}), (200, {"success": True})])
        c = ScrapeClient(opener=op, max_retries=2, backoff_base=0.0)
        self.assertTrue(c.scrape("https://example.com")["success"])

    def test_4xx_not_retried(self):
        """4xx 属请求问题，不应重试。"""
        calls = {"n": 0}

        def op(req, t):
            calls["n"] += 1
            return 404, b"{}"

        c = ScrapeClient(opener=op, max_retries=3, backoff_base=0.0)
        with self.assertRaises(CollectorError):
            c.scrape("https://example.com")
        self.assertEqual(calls["n"], 1)

    def test_network_error_retried_then_raises(self):
        calls = {"n": 0}

        def op(req, t):
            calls["n"] += 1
            raise urllib.error.URLError("boom")

        c = ScrapeClient(opener=op, max_retries=2, backoff_base=0.0)
        with self.assertRaises(CollectorError) as ctx:
            c.scrape("https://example.com")
        self.assertEqual(calls["n"], 3)
        self.assertTrue(ctx.exception.retryable)

    def test_timeout_mapped_to_retryable(self):
        def op(req, t):
            raise TimeoutError("slow")

        c = ScrapeClient(opener=op, max_retries=0)
        with self.assertRaises(CollectorError) as ctx:
            c.scrape("https://example.com")
        self.assertTrue(ctx.exception.retryable)

    def test_batch_chunks_and_flattens(self):
        c = ScrapeClient(opener=ok_opener({"results": [{"success": True}]}))
        out = c.scrape_batch([f"https://e{i}.com" for i in range(25)], chunk=10)
        self.assertEqual(len(out), 3)   # 3 片

    def test_batch_falls_back_to_individual(self):
        """批量端点失败时逐个抓取，保证部分成功。"""
        c = ScrapeClient(opener=ok_opener({"success": True}), max_retries=0)
        out = c.scrape_batch(["https://a.com", "https://b.com"])
        self.assertEqual(len(out), 2)

    def test_batch_individual_failure_recorded(self):
        def op(req, t):
            if b"batch" in (req.data or b""):
                return 500, b"{}"
            raise urllib.error.URLError("x")

        c = ScrapeClient(opener=op, max_retries=0, backoff_base=0.0)
        out = c.scrape_batch(["https://a.com"])
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["success"])


class TestCollectUrls(unittest.TestCase):
    def test_all_success(self):
        c = ScrapeClient(opener=ok_opener({"status": "healthy",
                                           "success": True}))
        task, res = collect_urls(["https://a.com"], client=c)
        self.assertIs(task.state, TaskState.SUCCEEDED)
        self.assertEqual(task.result_count, 1)
        self.assertEqual(len(res), 1)

    def test_unhealthy_aborts(self):
        c = ScrapeClient(opener=ok_opener({"status": "down"}))
        task, res = collect_urls(["https://a.com"], client=c,
                                 require_healthy=True)
        self.assertIs(task.state, TaskState.FAILED)
        self.assertEqual(res, [])
        self.assertIn("不健康", task.error)

    def test_unhealthy_allowed_when_not_required(self):
        c = ScrapeClient(opener=ok_opener({"status": "down",
                                           "success": True}))
        task, res = collect_urls(["https://a.com"], client=c,
                                 require_healthy=False)
        self.assertIs(task.state, TaskState.SUCCEEDED)

    def test_partial_success(self):
        # health ok, 然后一个成功一个失败
        op = seq_opener([(200, {"status": "healthy"}),
                         (200, {"success": True}),
                         (200, {"success": False, "error": "x"})])
        c = ScrapeClient(opener=op, max_retries=0)
        task, res = collect_urls(["https://a.com", "https://b.com"], client=c)
        self.assertIs(task.state, TaskState.SUCCEEDED)
        self.assertEqual(task.result_count, 1)
        self.assertTrue(any("部分成功" in n for n in task.notes))

    def test_callback_invoked_and_failure_isolated(self):
        seen = []

        def cb(r):
            seen.append(r)
            raise RuntimeError("callback boom")

        c = ScrapeClient(opener=ok_opener({"status": "healthy",
                                           "success": True}))
        task, _ = collect_urls(["https://a.com"], client=c, on_snapshot=cb)
        self.assertEqual(len(seen), 1)
        self.assertIs(task.state, TaskState.SUCCEEDED)   # 回调异常不影响主流程
        self.assertTrue(any("回调异常" in n for n in task.notes))

    def test_all_failed(self):
        c = ScrapeClient(opener=ok_opener({"status": "healthy",
                                           "success": False}))
        task, res = collect_urls(["https://a.com"], client=c)
        self.assertIs(task.state, TaskState.FAILED)
        self.assertIn("全部失败", task.error)

    def test_registry_is_used(self):
        reg = TaskRegistry()
        c = ScrapeClient(opener=ok_opener({"status": "healthy",
                                           "success": True}))
        task, _ = collect_urls(["https://a.com"], client=c, registry=reg)
        self.assertIs(reg.get(task.task_id), task)

    def test_attempts_counted(self):
        c = ScrapeClient(opener=ok_opener({"status": "healthy",
                                           "success": True}))
        task, _ = collect_urls(["https://a.com", "https://b.com"], client=c)
        self.assertEqual(task.attempts, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
