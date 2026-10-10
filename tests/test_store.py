#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""store 层单元测试（报告 §12.1 不可变快照）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import timedelta
from typing import Any, cast

from core.models import OddsSnapshot, SnapshotState, utcnow
from store import MemoryCache, RedisCache, SnapshotStore, make_cache, safe_name


def snap(match_id="2001", odds=(2.13, 3.45, 2.70), t=None,
         state=SnapshotState.ACTIVE, source="体彩官方API", league="日职",
         market="1X2"):
    return OddsSnapshot(
        match_id=match_id, league=league, home="鹿岛鹿角", away="大阪樱花",
        market=market, outcomes=("home", "draw", "away"), odds=odds,
        state=state, captured_at=t or utcnow(), source=source,
        metadata={"比赛日期": "2025-09-23"},
    )


class TestSafeName(unittest.TestCase):
    def test_path_traversal_blocked(self):
        for bad in ("../../etc/passwd", "..", "../a", "a/../../b", ""):
            out = safe_name(bad)
            self.assertNotIn("..", out)
            self.assertNotIn("/", out)
            self.assertNotEqual(out, "")

    def test_chinese_preserved(self):
        self.assertEqual(safe_name("日职"), "日职")
        self.assertEqual(safe_name("鹿岛鹿角_vs_大阪樱花"), "鹿岛鹿角_vs_大阪樱花")

    def test_length_capped(self):
        self.assertLessEqual(len(safe_name("x" * 500)), 80)

    def test_non_str_input(self):
        # 本用例的**目的**就是传入非 str 值，验证内部会自行 str() 归一。
        # 用 cast(Any, ...) 显式声明“故意绕过类型声明”，
        # 避免类型检查器把它当成调用错误。
        self.assertEqual(safe_name(cast(Any, 12345)), "12345")
        self.assertEqual(safe_name(cast(Any, None)), "None")


class TestMemoryCache(unittest.TestCase):
    def test_set_get_delete(self):
        c = MemoryCache()
        c.set("k", "v")
        self.assertEqual(c.get("k"), "v")
        c.delete("k")
        self.assertIsNone(c.get("k"))

    def test_missing_returns_none(self):
        self.assertIsNone(MemoryCache().get("nope"))

    def test_keys_prefix(self):
        c = MemoryCache()
        c.set("a:1", "x")
        c.set("a:2", "y")
        c.set("b:1", "z")
        self.assertEqual(c.keys("a:"), ["a:1", "a:2"])

    def test_clear(self):
        c = MemoryCache()
        c.set("k", "v")
        c.clear()
        self.assertIsNone(c.get("k"))

    def test_ttl_expiry(self):
        import time
        c = MemoryCache()
        c.set("k", "v", ttl=1)
        self.assertEqual(c.get("k"), "v")
        time.sleep(1.1)
        self.assertIsNone(c.get("k"))

    def test_available(self):
        self.assertTrue(MemoryCache().available())


class TestRedisCache(unittest.TestCase):
    def test_falls_back_when_unavailable(self):
        """无 Redis 时必须安全降级（available()=False 且不抛异常）。"""
        c = RedisCache("redis://127.0.0.1:1/0", timeout=0.2)
        self.assertFalse(c.available())
        self.assertIsNone(c.get("k"))
        c.set("k", "v")            # 不应抛
        c.delete("k")
        self.assertEqual(c.keys(), [])
        c.clear()

    def test_make_cache_memory_forced(self):
        c = make_cache(prefer_redis=False)
        self.assertIsInstance(c, MemoryCache)

    def test_make_cache_env_override(self):
        old = os.environ.get("CACHE_BACKEND")
        os.environ["CACHE_BACKEND"] = "memory"
        try:
            self.assertIsInstance(make_cache(), MemoryCache)
        finally:
            if old is None:
                os.environ.pop("CACHE_BACKEND", None)
            else:
                os.environ["CACHE_BACKEND"] = old


class TestSnapshotStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="snapstore_")
        self.store = SnapshotStore(self.tmp, cache=MemoryCache())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_append_and_load(self):
        p = self.store.append(snap())
        self.assertTrue(os.path.isfile(p))
        got = self.store.load_match("体彩官方API", "日职", "2001")
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0].odds[0], 2.13, places=6)

    def test_immutability_same_snapshot_creates_new_file(self):
        """报告 §12.1：快照一经写入不可修改，重复写入产生新文件。"""
        s = snap()
        p1 = self.store.append(s)
        p2 = self.store.append(s)
        self.assertNotEqual(p1, p2)
        self.assertEqual(len(self.store.load_match("体彩官方API", "日职", "2001")), 2)

    def test_time_ordering(self):
        t0 = utcnow()
        self.store.append(snap(odds=(2.0, 3.0, 4.0), t=t0))
        self.store.append(snap(odds=(2.2, 3.0, 4.0), t=t0 + timedelta(seconds=30)))
        self.store.append(snap(odds=(2.1, 3.0, 4.0), t=t0 + timedelta(seconds=15)))
        got = self.store.load_match("体彩官方API", "日职", "2001")
        self.assertEqual([s.odds[0] for s in got], [2.0, 2.1, 2.2])

    def test_state_preserved(self):
        self.store.append(snap(state=SnapshotState.SUSPENDED))
        got = self.store.load_match("体彩官方API", "日职", "2001")
        self.assertIs(got[0].state, SnapshotState.SUSPENDED)

    def test_index_written(self):
        self.store.append(snap())
        d = self.store.match_dir(snap())
        idx = d / "_index.json"
        self.assertTrue(idx.is_file())
        payload = json.loads(idx.read_text(encoding="utf-8"))
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["match_id"], "2001")

    def test_load_missing_returns_empty(self):
        self.assertEqual(self.store.load_match("无", "无", "9999"), [])

    def test_corrupt_file_skipped(self):
        p = self.store.append(snap())
        (p.parent / "broken.json").write_text("{not json", encoding="utf-8")
        got = self.store.load_match("体彩官方API", "日职", "2001")
        self.assertEqual(len(got), 1)  # 损坏文件被跳过，不影响其它

    def test_atomic_write_no_temp_left(self):
        self.store.append(snap())
        left = [f for f in os.listdir(self.store.match_dir(snap()))
                if f.startswith(".tmp_")]
        self.assertEqual(left, [])

    def test_snapshot_json_is_valid(self):
        p = self.store.append(snap())
        payload = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(payload["_schema"], "odds_snapshot/v1")
        self.assertIn("booksum", payload)

    def test_cache_invalidated_on_append(self):
        self.store.cache_put("2001", [snap()])
        self.assertIsNotNone(self.store.cache.get(self.store.cache_key("2001")))
        self.store.append(snap())
        self.assertIsNone(self.store.cache.get(self.store.cache_key("2001")))

    def test_stats(self):
        self.store.append(snap())
        st = self.store.stats()
        self.assertEqual(st["match_dirs"], 1)
        self.assertEqual(st["snapshot_files"], 1)
        self.assertEqual(st["cache_backend"], "memory")

    def test_stats_keep_match_indexes_after_s3_removes_snapshot_files(self):
        snapshot = snap()
        path = self.store.append(snapshot)
        path.unlink()
        metadata = self.store.root / 'unindexed'
        metadata.mkdir()
        (metadata / 'sample.json').write_text('{}')
        (metadata / 'ignored.txt').write_text('other')
        stats = self.store.stats()
        self.assertEqual(stats['match_dirs'], 1)
        self.assertEqual(stats['snapshot_files'], 1)

    def test_multi_source_separated(self):
        self.store.append(snap(source="源A"))
        self.store.append(snap(source="源B"))
        self.assertEqual(len(self.store.load_match("源A", "日职", "2001")), 1)
        self.assertEqual(len(self.store.load_match("源B", "日职", "2001")), 1)

    def test_no_global_match_scan_method(self):
        """确认工具方法存在（service 层用于全量扫描）。"""
        self.store.append(snap())
        self.assertTrue(hasattr(self.store, "_load_dir"))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestCachePreference(unittest.TestCase):
    """缓存后端偏好的自动判定（修复「容器内永远用不到 Redis」的缺陷）。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="cachepref_")
        self._env = {k: os.environ.get(k) for k in ("REDIS_URL", "CACHE_BACKEND")}

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_no_redis_url_falls_back_to_memory(self):
        from service.valuation import ValuationService
        os.environ.pop("REDIS_URL", None)
        os.environ.pop("CACHE_BACKEND", None)
        svc = ValuationService(snapshot_root=self._tmp)
        self.assertEqual(svc.store.cache.name, "memory")

    def test_explicit_memory_backend_wins(self):
        from service.valuation import ValuationService
        os.environ["REDIS_URL"] = "redis://127.0.0.1:1/0"
        os.environ["CACHE_BACKEND"] = "memory"
        svc = ValuationService(snapshot_root=self._tmp)
        self.assertEqual(svc.store.cache.name, "memory")

    def test_redis_url_enables_redis_preference(self):
        """设了 REDIS_URL 时，服务必须以 prefer_redis=True 调用 make_cache。

        这是缺陷回归测试：此前 service 硬编码 prefer_redis=False，
        导致容器内即使装了 redis 库、Redis 可达，也永远用内存缓存
        （缓存不跨进程）。这里用 mock 断言**传参**，
        从而不依赖真实 Redis 是否可达。
        """
        from unittest import mock

        import service.valuation as sv

        os.environ["REDIS_URL"] = "redis://example:6379/0"
        os.environ.pop("CACHE_BACKEND", None)
        with mock.patch.object(sv, "make_cache",
                               return_value=MemoryCache()) as m:
            sv.ValuationService(snapshot_root=self._tmp)
        self.assertTrue(m.called)
        self.assertIs(m.call_args.kwargs.get("prefer_redis"), True)

    def test_unreachable_redis_degrades_gracefully(self):
        """Redis 不可达时必须优雅降级到内存，而非启动失败。"""
        from service.valuation import ValuationService
        os.environ["REDIS_URL"] = "redis://127.0.0.1:1/0"
        os.environ.pop("CACHE_BACKEND", None)
        svc = ValuationService(snapshot_root=self._tmp)
        self.assertIn(svc.store.cache.name, ("memory", "redis"))
        svc.store.cache.set("k", "v")   # 任何后端都应可用

    def test_explicit_prefer_redis_flag(self):
        from service.valuation import ValuationService
        svc = ValuationService(snapshot_root=self._tmp, prefer_redis=False)
        self.assertEqual(svc.store.cache.name, "memory")
