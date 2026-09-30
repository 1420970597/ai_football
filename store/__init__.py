#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
store —— 存储层（不可变快照 + 缓存）

AGENTS.md §3.2：所有数据库与缓存访问必须收敛在本层，
严禁把存储逻辑散落在 API 或采集代码中。

模块：
  snapshot_store  SnapshotStore（不可变快照，报告 §12.1）
                  CacheBackend / MemoryCache / RedisCache
"""

from .snapshot_store import (
    CacheBackend,
    MemoryCache,
    RedisCache,
    SnapshotStore,
    make_cache,
    safe_name,
)

__all__ = [
    "CacheBackend",
    "MemoryCache",
    "RedisCache",
    "SnapshotStore",
    "make_cache",
    "safe_name",
]
