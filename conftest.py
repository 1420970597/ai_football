#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pytest 全局配置。

扁平布局应用（非已安装包）需要在测试时把仓库根加入 sys.path，
否则 `import core` / `import store` / `import collector` 会失败。

这也是 AGENTS.md §6 第 3 步「容器内跑 pytest」能直接工作的前提：
容器里同样不安装本包，仅挂载源码。
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
