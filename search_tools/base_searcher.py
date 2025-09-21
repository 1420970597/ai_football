#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
搜索器基类
"""

from abc import ABC, abstractmethod
from typing import List, Dict

class BaseSearcher(ABC):
    """搜索器基类"""

    @abstractmethod
    def search_recent_articles(self, keyword: str, hours: int = 48, max_pages: int = 10) -> List[Dict]:
        """
        搜索最近指定小时内的文章

        Args:
            keyword: 搜索关键词
            hours: 时间范围（小时），默认48小时
            max_pages: 最大搜索页数

        Returns:
            List[Dict]: 最近文章列表
        """
        pass
