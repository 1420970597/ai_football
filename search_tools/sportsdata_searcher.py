#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SportsDataIO文章搜索器
"""

import requests
from typing import List, Dict
from .base_searcher import BaseSearcher

class SportsDataSearcher(BaseSearcher):
    """SportsDataIO文章搜索器"""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.sportsdata.io/v3/soccer/news/json/News"
        self.headers = {
            'Ocp-Apim-Subscription-Key': self.api_key
        }

    def search_recent_articles(self, keyword: str, hours: int = 48, max_pages: int = 10) -> List[Dict]:
        """
        使用SportsDataIO API搜索最近的文章。
        注意：SportsDataIO的免费版API可能不支持按关键词搜索，
        这里我们将获取最新的新闻，然后在本地进行筛选。
        """
        print(f"正在从SportsDataIO获取最近的新闻...")

        try:
            response = requests.get(self.base_url, headers=self.headers, timeout=10)
            response.raise_for_status()

            all_news = response.json()

            # 在本地根据关键词筛选
            filtered_articles = [
                article for article in all_news
                if keyword.lower() in article.get('Title', '').lower() or
                   keyword.lower() in article.get('Content', '').lower()
            ]

            # 转换成我们需要的格式
            articles = []
            for item in filtered_articles:
                articles.append({
                    'title': item.get('Title'),
                    'url': item.get('Url'),
                    'summary': item.get('Content'),
                    'author': item.get('OriginalSource'),
                    'publish_time': item.get('TimeAgo'),
                    'source': 'SportsDataIO'
                })

            print(f"从SportsDataIO找到 {len(articles)} 篇相关文章")
            return articles

        except requests.RequestException as e:
            print(f"请求SportsDataIO API失败: {e}")
            return []
