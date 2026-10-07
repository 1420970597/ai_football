#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collector —— 采集与归一化层（L1）

AGENTS.md §3.2：本层负责「把外部原始数据变成领域模型」。
数据源**只有乐鱼**，一律走 HTTP / WebSocket，**不涉及浏览器**。

模块：
  leyu_normalizer  乐鱼上游 JSON → OddsSnapshot
  leyu_client      乐鱼业务 API 客户端（零依赖 urllib）
  leyu_ws          乐鱼实时赔率 WebSocket 推送
"""

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
    "DecodeError",
    "LEYUClient",
    "LEYUError",
    "LEYUFeed",
    "LEYUMatch",
    "MarketQuote",
    "OddsQuote",
    "TransportError",
    "WebSocketConnection",
    "decode_envelope",
    "decode_prod_json",
    "parse_match_list",
    "parse_odds_block",
]
