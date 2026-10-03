#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collector —— 采集与归一化层（L1）

AGENTS.md §3.2：本层负责「把外部原始数据变成领域模型」，
但**绝不直接操作浏览器**——浏览器能力一律经 HTTP 委托给 browser-scraper 容器。

模块：
  normalizer    上游 JSON → OddsSnapshot（容忍脏数据，告警不中断）
  orchestrator  采集编排 + 任务状态机 + ScrapeClient（零依赖 HTTP）
"""

from .normalizer import (
    MARKET_1X2,
    MARKET_HANDICAP,
    MapResult,
    ParseIssue,
    normalize_matches,
    parse_match,
)
from .orchestrator import (
    CollectTask,
    CollectorError,
    HealthResult,
    ScrapeClient,
    TaskRegistry,
    TaskState,
    collect_urls,
)
from .leyu_client import (
    LEYUClient,
    LEYUError,
    LEYUMatch,
    MarketQuote,
    OddsQuote,
    TransportError,
    DecodeError,
    decode_envelope,
    decode_prod_json,
    parse_match_list,
    parse_odds_block,
)
from .leyu_ws import LEYUFeed, WebSocketConnection

__all__ = [
    "MARKET_1X2",
    "MARKET_HANDICAP",
    "CollectTask",
    "CollectorError",
    "DecodeError",
    "HealthResult",
    "LEYUClient",
    "LEYUError",
    "LEYUFeed",
    "LEYUMatch",
    "MapResult",
    "MarketQuote",
    "OddsQuote",
    "ParseIssue",
    "ScrapeClient",
    "TaskRegistry",
    "TaskState",
    "TransportError",
    "WebSocketConnection",
    "collect_urls",
    "decode_envelope",
    "decode_prod_json",
    "normalize_matches",
    "parse_match",
    "parse_match_list",
    "parse_odds_block",
]
