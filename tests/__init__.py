#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests —— 单元测试包（AGENTS.md §4.3：每个业务模块必须配套测试）"""

from __future__ import annotations

import types
from typing import Any, Set


def referenced_names(fn: Any) -> Set[str]:
    """收集函数**编译后**引用到的全部名字（含其内部嵌套函数与 lambda）。

    为何不用 `inspect.getsource()`：它用**编译时冻结的行号**去读**当前磁盘
    文件**，所以只要文件在测试运行期间被整体重排（自动格式化、后台 Agent
    写文件、多人共写同一工作区），就会返回**别的函数体** —— 既能造成
    **假失败**，也能造成**假通过**（实测：一次 `ruff format` 让两个
    "必须调用 `_record_ledger`" 的断言报出 `candidates` 函数体）。

    为何比 `getsource` 更强：`assertIn("名字", 源码文本)` 连**注释与字符串**
    里的提及都算满足（假通过）；本函数只看代码对象里真实的
    `co_names`（全局/属性引用），即“确实在代码里用了它”。

    必须递归 `co_consts`：`_start_background` 里 `live_match_ids` 只在嵌套
    函数 `_mids()` 内部被引用，外层 `co_names` 并不包含它。

    刻意**不**收集 `co_varnames`：局部变量叫同名不代表“用了它”，
    收进来会重新引入假通过。

    Args:
        fn: 任意 Python 函数（含方法、静态方法取出的函数对象）。

    Returns:
        引用到的名字集合。
    """
    seen: Set[int] = set()
    out: Set[str] = set()

    def walk(code: types.CodeType) -> None:
        if id(code) in seen:
            return
        seen.add(id(code))
        out.update(code.co_names)
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                walk(const)

    walk(fn.__code__)
    return out
