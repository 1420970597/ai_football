#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core —— 领域核心层（L2 特征 + L1′ 微结构 + L4 经济学算法栈 + L6 校准）

设计约束（AGENTS.md §1/§3.2）：
  - **仅标准库**：宿主机无 pip 且依赖不全，核心算法必须零依赖可直接运行与测试
  - **纯函数式**：本层不做任何 I/O（网络/文件/数据库），便于单测与复用
  - 所有结论可追溯到 reports/leyu-kaiyun-odds-bot-feasibility/REPORT.md

模块：
  models       数据模型（不可变 frozen dataclass）
  devig        去水五法（报告 §8.1）
  economics    优势估计 / 收缩 / Kelly / 成本（报告 §8.2–8.7）
  calibration  校准指标（报告 §8.8）
  microstructure  微结构信号（报告 §5.1）
"""

from .models import (
    CalibrationReport,
    DevigMethod,
    EdgeResult,
    ExecutionFilter,
    FairProbabilities,
    MicrostructureSignal,
    OddsSnapshot,
    SizingResult,
    SnapshotState,
)

__all__ = [
    "CalibrationReport",
    "DevigMethod",
    "EdgeResult",
    "ExecutionFilter",
    "FairProbabilities",
    "MicrostructureSignal",
    "OddsSnapshot",
    "SizingResult",
    "SnapshotState",
]
