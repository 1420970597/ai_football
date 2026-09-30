#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
service —— 业务编排层

职责：把 core（纯算法）与 store（存储）编排成面向用例的服务，
供 api 层调用。本层不处理 HTTP，也不直接操作浏览器。

模块：
  valuation  ValuationService（快照→去水→优势→仓位→校准）
"""

from .valuation import DEFAULT_SOURCE, ValuationService, load_corpus

__all__ = ["DEFAULT_SOURCE", "ValuationService", "load_corpus"]
