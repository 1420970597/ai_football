#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快照存储层的性能/并发语义回归测试。

覆盖 `ValuationService` 的三个**真实性能故障**（均为已修回归）：
  1. 新快照必须**增量合并**进缓存，而不是整库失效重扫（14~15s）；
  2. 存储指纹 TTL 必须真的生效，否则每场决策都 rglob 全库（CPU 打满）；
  3. `/health` 的存储统计必须**单飞**，否则探针把服务探死。

数据用**真实乐鱼快照离线夹具**（`tests/fixtures/leyu_snapshots`），
不依赖仓库 `output/`，也不需要联网。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, List

from service.valuation import LEYU_SOURCE_NAME, ValuationService
from store import safe_name


#: 真实乐鱼快照离线夹具（数据源已统一为乐鱼，不再有体彩语料）。
FIXTURE_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fixtures", "leyu_snapshots")


def _leyu_snapshot_root(case: unittest.TestCase) -> str:
    """把乐鱼快照夹具复制到临时目录，返回可作 snapshot_root 的路径。

    复制而非直接引用：用例会调 `merge_snapshots()` 等写路径，
    直接引用会污染仓库里的夹具。
    """
    tmp = tempfile.mkdtemp(prefix="multimkt_")
    case.addCleanup(shutil.rmtree, tmp, True)
    root = os.path.join(tmp, "snapshots")
    shutil.copytree(os.path.join(FIXTURE_ROOT, LEYU_SOURCE_NAME),
                    os.path.join(root, LEYU_SOURCE_NAME))
    return root


class TestSnapshotCacheMerge(unittest.TestCase):
    """回归：新快照必须**增量合并**进缓存，而不是整体失效。

    真实性能故障：全量重扫 65,942 个快照文件需 **14~15 秒**（实测）。
    而 `refresh_matches()` 在“盘口变动触发决策”下会被持续高频调用；
    早期实现每次都 `invalidate_cache()` → 下一批决策重扫全库 →
    容器 CPU 打满 99%、`/health` 被拖到 30s+ 超时，服务形同挂死。

    `merge_snapshots()` 把本批新快照并入缓存（O(本批条数)），
    并刷新存储指纹，使合并结果被确认有效（不会立刻又被重扫）。
    """

    def _svc(self) -> ValuationService:
        return ValuationService(snapshot_root=_leyu_snapshot_root(self),
                                prefer_redis=False)

    def test_merge_updates_cache_without_full_reload(self) -> None:
        from core.models import OddsSnapshot, utcnow

        svc = self._svc()
        # 先建立缓存
        base = svc._all_snapshots()
        self.assertIsNotNone(svc._snap_cache)
        # ⚠️ `_all_snapshots()` 返回的是**内部缓存列表本身**（非拷贝），
        # 因此必须在 merge 前先记下长度，否则 merge 追加后 len(base)
        # 会跟着一起变（这正是下面的断言曾误报的原因）。
        before_len = len(base)

        fresh = OddsSnapshot(
            match_id="__merge_test__", league="测试", home="A", away="B",
            market="HAD", outcomes=("home", "draw", "away"),
            odds=(2.0, 3.3, 3.6), source=svc.source.display_source,
            captured_at=utcnow())
        n = svc.merge_snapshots([fresh])
        self.assertEqual(n, 0)                     # 新键，非替换

        # 缓存已包含新数据（无需重扫磁盘）
        cache = svc._snap_cache
        assert cache is not None
        self.assertIn("__merge_test__", {s.match_id for s in cache})
        self.assertEqual(len(cache), before_len + 1)

    def test_merge_replaces_same_key(self) -> None:
        from core.models import OddsSnapshot, utcnow

        svc = self._svc()
        svc._all_snapshots()
        when = utcnow()
        a = OddsSnapshot(match_id="__m2__", league="L", home="A", away="B",
                         market="HAD", outcomes=("home", "draw", "away"),
                         odds=(2.0, 3.3, 3.6), captured_at=when,
                         source=svc.source.display_source)
        b = OddsSnapshot(match_id="__m2__", league="L", home="A", away="B",
                         market="HAD", outcomes=("home", "draw", "away"),
                         odds=(1.5, 3.9, 4.2), captured_at=when,
                         source=svc.source.display_source)
        svc.merge_snapshots([a])
        self.assertEqual(svc.merge_snapshots([b]), 1)   # 同键 → 替换
        cache = svc._snap_cache
        assert cache is not None
        got = [s for s in cache if s.match_id == "__m2__"]
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0].odds[0], 1.5)

    def test_merge_without_cache_is_noop(self) -> None:
        from core.models import OddsSnapshot, utcnow

        svc = self._svc()
        svc._snap_cache = None                      # 模拟缓存尚未建立
        s = OddsSnapshot(match_id="__m3__", league="L", home="A", away="B",
                         market="HAD", outcomes=("home", "draw", "away"),
                         odds=(2.0, 3.3, 3.6), captured_at=utcnow(),
                         source=svc.source.display_source)
        self.assertEqual(svc.merge_snapshots([s]), 0)

    def test_stamp_refreshed_so_cache_is_reused(self) -> None:
        """合并后指纹必须与磁盘一致，否则下一次读取又重扫全库。"""
        svc = self._svc()
        svc._all_snapshots()
        self.assertIsNotNone(svc._snap_stamp)
        from core.models import OddsSnapshot, utcnow
        svc.merge_snapshots([OddsSnapshot(
            match_id="__m4__", league="L", home="A", away="B", market="HAD",
            outcomes=("home", "draw", "away"), odds=(2.0, 3.3, 3.6),
            captured_at=utcnow(), source=svc.source.display_source)])
        self.assertIsNotNone(svc._snap_stamp)
        before = svc._snap_cache
        again = svc._all_snapshots()
        self.assertIs(again, before)               # 命中缓存，未重建


class TestStoreStampTtlActuallyWorks(unittest.TestCase):
    """**回归**：存储指纹的 TTL 必须真的生效（否则 CPU 被打满）。

    真实性能缺陷：`_store_stamp_cached` 早期只在 `_snap_stamp is None` 时
    记录时间戳，而缓存一旦建好 `_snap_stamp` 就不是 None → `_snap_stamp_at`
    永远停在最初那一刻 → `now - at` 持续增长 → TTL 形同虚设 →
    **每次调用都 rglob 全目录**（3111 个 `_index.json`，实测 0.55s）。

    后果：结算逐场 34 次 ≈ 19s、决策逐场上千次 → py-spy 拍到
    `analysis-settle`/`analysis-cycle` 卡在 `_store_stamp → rglob`，
    容器 CPU 抬到 100%+、接口超时（用户报「加载慢」）。
    """

    def _svc(self) -> ValuationService:
        return ValuationService(snapshot_root=_leyu_snapshot_root(self),
                                prefer_redis=False)

    def test_repeated_calls_hit_ttl(self) -> None:
        svc = self._svc()
        base = svc.store.root / safe_name(svc.source.display_source)
        first = svc._store_stamp_cached(base)          # 冷：真扫一次
        t0 = time.perf_counter()
        for _ in range(20):
            again = svc._store_stamp_cached(base)
        elapsed = time.perf_counter() - t0
        self.assertEqual(first, again, "同一 TTL 内应返回同一指纹")
        # 20 次命中应当远快于 20 次真实扫描（单次扫描约 0.5s）
        self.assertLess(elapsed, 0.5,
                        "TTL 未生效：仍在反复全目录扫描（耗时 %.3fs）" % elapsed)

    def test_timestamp_is_refreshed(self) -> None:
        """每次真实扫描后都应刷新时间戳，否则 TTL 永远过期。"""
        svc = self._svc()
        base = svc.store.root / safe_name(svc.source.display_source)
        svc._store_stamp_cached(base)
        first_at = svc._snap_stamp_at
        self.assertGreater(first_at, 0.0, "必须记录扫描时刻")
        # 把时间戳人为拨回过去 → 下一次应触发真实扫描并刷新它
        svc._snap_stamp_at = first_at - 10_000.0
        svc._store_stamp_cached(base)
        self.assertGreater(svc._snap_stamp_at, first_at - 10_000.0,
                           "真实扫描后必须刷新时间戳")


class TestStoreStatsSingleFlight(unittest.TestCase):
    """**回归**：`/health` 的存储统计必须**单飞**（否则探针把服务探死）。

    实测真实事故：`store.stats()` 递归统计 8+ 万个文件需 **3s 以上**。
    早期实现把计算放在锁**外**：

        with lock:
            if 缓存有效: return 缓存
        stats = self.store.stats()      # ← 锁外，多个线程同时跑
        with lock:
            写回

    于是并发请求（Docker 健康检查每 20s + `/matches` 等）会**同时**发现
    缓存过期、**同时**全盘遍历 —— py-spy 实测拍到 5+ 个请求线程同时卡在
    `_store_stats_cached`，CPU 吃满、健康检查自己超时（exit=-1，
    容器被判 unhealthy，探针把服务探死）。

    修法：把“检查 + 计算 + 写回”放进**同一把锁**，只让一个线程真扫盘。
    """

    def _svc(self) -> ValuationService:
        # 用**临时空目录**：本用例验证的是并发语义，不该依赖 8 万文件的语料
        # （那样一次测试要跑数秒，既慢又不稳定）。
        tmp = tempfile.mkdtemp(prefix="statssf_")
        self.addCleanup(shutil.rmtree, tmp, True)
        return ValuationService(snapshot_root=tmp, prefer_redis=False)

    def test_concurrent_calls_scan_once(self) -> None:
        """并发 8 次只真正扫盘 1 次。

        用 `threading.Barrier` 让 8 个线程**同时**发起调用
        （不用 `sleep` 凑时序：那既慢又不确定）；
        核心不变式是**扫盘次数 == 1** —— 修复前会是 8
        （各自全盘遍历，叠加成十几秒，正是健康检查超时的原因）。
        """
        svc = self._svc()
        calls: List[int] = []
        real = svc.store.stats

        def _counting() -> Dict[str, Any]:
            calls.append(1)
            return real()

        svc.store.stats = _counting
        n = 8
        gate = threading.Barrier(n)     # 让所有线程在同一点同时冲进调用
        results: List[Any] = []
        lock = threading.Lock()

        def _work() -> None:
            gate.wait()
            r = svc._store_stats_cached()
            with lock:
                results.append(r)

        threads = [threading.Thread(target=_work) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert svc._stats_thread is not None
        svc._stats_thread.join(timeout=5)

        self.assertEqual(len(results), n)
        self.assertEqual(len(calls), 1,
                         "并发调用只应真正扫盘一次（实测 %d 次）" % len(calls))

    def test_cached_after_first_call(self) -> None:
        svc = self._svc()
        svc._store_stats_cached()
        assert svc._stats_thread is not None
        svc._stats_thread.join(timeout=5)
        first = svc._store_stats_cached()
        second = svc._store_stats_cached()
        self.assertEqual(first, second)
        self.assertIs(second, svc._stats_cache)

    def test_cold_health_returns_while_statistics_are_blocked(self) -> None:
        svc = self._svc()
        entered, release = threading.Event(), threading.Event()
        def blocked():
            entered.set()
            release.wait(5)
            return {'snapshot_files': 123}
        svc.store.stats = blocked
        try:
            result = svc.health()
            self.assertEqual(result['snapshot_store']['statistics_state'], 'loading')
            self.assertTrue(entered.wait(1))
            self.assertEqual(svc.health()['status'], 'healthy')
        finally:
            release.set()
            assert svc._stats_thread is not None
            svc._stats_thread.join(timeout=5)
        self.assertEqual(svc.health()['snapshot_store']['snapshot_files'], 123)
