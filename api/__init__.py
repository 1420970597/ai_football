#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
api —— REST 接入层（B/S 架构的 Server 端入口）

AGENTS.md §3.2：本层**只做** HTTP 编解码、入参校验与上下文装配，
严禁在此编写业务逻辑或直接访问存储。

模块：
  app  create_app() 工厂 + 全部端点（README §6 契约）
"""
