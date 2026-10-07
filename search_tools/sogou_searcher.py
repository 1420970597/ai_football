#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信公众号文章搜索器
基于搜狗微信搜索API实现关键词搜索文章功能
"""

import requests
from bs4 import BeautifulSoup
import re
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional


from .base_searcher import BaseSearcher

class SogouSearcher(BaseSearcher):
    """搜狗微信公众号文章搜索器"""

    def __init__(self):
        self.base_url = "https://weixin.sogou.com/weixin"
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            # 添加有效Cookie来解除爬虫限制
            'Cookie': 'IPLOC=CN1100; SUID=09C6F4784752A20B0000000068CD4DD3; cuid=AAE885UYVgAAAAuiU6KS+gAASQU=; SUV=1758285268292182; ABTEST=5|1758285295|v1; SNUID=71BE8D00787E475ECB093D817998205E; weixinIndexVisited=1; ariaDefaultTheme=undefined'
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        print("已设置有效Cookie，应能解除爬虫限制")

    def search_articles(self, keyword: str, page: int = 1) -> List[Dict]:
        """
        搜索微信公众号文章

        Args:
            keyword: 搜索关键词
            page: 页码，默认为1

        Returns:
            List[Dict]: 文章信息列表
        """
        params = {
            'query': keyword,
            'type': '2',  # 文章类型
            'ie': 'utf8',
            'page': str(page),
            's_from': 'input',
            '_sug_': 'n',
            '_sug_type_': '1'
        }

        try:
            response = self.session.get(self.base_url, params=params, timeout=10)
            response.raise_for_status()
            response.encoding = 'utf-8'

            return self._parse_articles(response.text, keyword)

        except requests.RequestException as e:
            print(f"请求失败: {e}")
            return []

    def _parse_articles(self, html: str, keyword: str) -> List[Dict]:
        """
        解析HTML页面，提取文章信息

        Args:
            html: HTML页面内容
            keyword: 搜索关键词

        Returns:
            List[Dict]: 文章信息列表
        """
        soup = BeautifulSoup(html, 'html.parser')
        articles = []

        # 查找文章列表容器
        news_list = soup.find('ul', class_='news-list')
        if not news_list:
            return articles

        # 遍历文章列表项
        for li in news_list.find_all('li', id=re.compile(r'sogou_vr_\d+_box_\d+')):
            article_info = self._extract_article_info(li)
            if article_info:
                articles.append(article_info)

        return articles

    def _extract_article_info(self, li_element) -> Optional[Dict]:
        """
        从li元素中提取文章信息

        Args:
            li_element: 文章对应的li元素

        Returns:
            Optional[Dict]: 文章信息字典
        """
        try:
            article = {}

            # 提取标题和链接
            title_element = li_element.find('h3')
            if title_element:
                title_link = title_element.find('a')
                if title_link:
                    # 清理标题中的HTML标签（如高亮标签）
                    article['title'] = self._clean_text(title_link.get_text())
                    article['url'] = title_link.get('href', '')

                    # 如果是相对链接，补充完整URL
                    if article['url'].startswith('/link?'):
                        article['url'] = 'https://weixin.sogou.com' + article['url']

            # 提取摘要
            summary_element = li_element.find('p', class_='txt-info')
            if summary_element:
                article['summary'] = self._clean_text(summary_element.get_text())

            # 提取发布者和时间信息
            s_p_element = li_element.find('div', class_='s-p')
            if s_p_element:
                spans = s_p_element.find_all('span')
                if len(spans) >= 2:
                    article['author'] = spans[0].get_text().strip()

                    # 提取时间戳并转换
                    time_script = spans[1].find('script')
                    if time_script:
                        timestamp_match = re.search(r"timeConvert\('(\d+)'\)", time_script.get_text())
                        if timestamp_match:
                            timestamp = int(timestamp_match.group(1))
                            publish_datetime = self._convert_timestamp(timestamp)
                            article['publish_datetime'] = publish_datetime
                            article['publish_time'] = self._format_datetime(publish_datetime)

            # 提取图片URL
            img_element = li_element.find('img')
            if img_element:
                article['image_url'] = img_element.get('src', '')

            return article if 'title' in article else None

        except Exception as e:
            print(f"解析文章信息时出错: {e}")
            return None

    def _clean_text(self, text: str) -> str:
        """
        清理文本内容，移除多余的空白字符

        Args:
            text: 原始文本

        Returns:
            str: 清理后的文本
        """
        # 移除多余的空白字符和换行符
        cleaned = re.sub(r'\s+', ' ', text.strip())
        return cleaned

    def _convert_timestamp(self, timestamp: int) -> Optional[datetime]:
        """
        转换时间戳为datetime对象

        Args:
            timestamp: 时间戳（秒或毫秒）

        Returns:
            datetime: 时间对象；无法解析时返回 None
        """
        try:
            # 将毫秒时间戳转换为秒
            ts: float = float(timestamp)
            if len(str(timestamp)) == 13:  # 毫秒时间戳
                ts = ts / 1000

            return datetime.fromtimestamp(ts)
        except (ValueError, OSError, OverflowError, TypeError):
            return None

    def _format_datetime(self, dt: Optional[datetime]) -> str:
        """
        格式化datetime对象为字符串

        Args:
            dt: datetime对象

        Returns:
            str: 格式化的时间字符串
        """
        if dt is None:
            return "时间解析失败"
        return dt.strftime('%Y-%m-%d %H:%M:%S')

    def filter_recent_articles(self, articles: List[Dict], hours: int = 48) -> List[Dict]:
        """
        过滤最近指定小时内的文章

        Args:
            articles: 文章列表
            hours: 时间范围（小时），默认48小时

        Returns:
            List[Dict]: 过滤后的文章列表
        """
        now = datetime.now()
        cutoff_time = now - timedelta(hours=hours)

        recent_articles = []
        for article in articles:
            publish_datetime = article.get('publish_datetime')
            if publish_datetime and publish_datetime >= cutoff_time:
                recent_articles.append(article)

        return recent_articles

    def search_with_pagination(self, keyword: str, max_pages: int = 3, filter_hours: Optional[int] = None) -> List[Dict]:
        """
        多页搜索文章

        Args:
            keyword: 搜索关键词
            max_pages: 最大搜索页数
            filter_hours: 时间过滤范围（小时），如果为None则不过滤

        Returns:
            List[Dict]: 所有页面的文章信息列表
        """
        all_articles = []

        for page in range(1, max_pages + 1):
            print(f"正在搜索第 {page} 页...")
            articles = self.search_articles(keyword, page)

            if not articles:
                print(f"第 {page} 页没有找到文章，停止搜索")
                break

            # 如果设置了时间过滤，应用过滤
            if filter_hours is not None:
                articles = self.filter_recent_articles(articles, filter_hours)

            all_articles.extend(articles)

            # 为了避免请求过于频繁，添加延时
            time.sleep(1)

        return all_articles

    def search_recent_articles_force(self, keyword: str, hours: int = 48, max_pages: int = 10) -> List[Dict]:
        """
        强制搜索指定页数，然后筛选最近指定小时内的文章
        不管中间页面是否有最近文章，都会搜索完所有指定页面

        Args:
            keyword: 搜索关键词
            hours: 时间范围（小时），默认48小时
            max_pages: 强制搜索的页数，默认10页

        Returns:
            List[Dict]: 最近文章列表
        """
        print(f"正在强制搜索{max_pages}页，然后筛选最近{hours}小时内关于'{keyword}'的文章...")

        all_articles = []
        recent_articles = []
        failed_pages = 0

        for page in range(1, max_pages + 1):
            print(f"正在搜索第 {page} 页...")
            articles = self.search_articles(keyword, page)

            if not articles:
                print(f"第 {page} 页没有找到文章")
                failed_pages += 1
                # 如果连续3页都没有文章，可能是到了搜索结果的末尾
                if failed_pages >= 3:
                    print(f"连续{failed_pages}页无结果，可能已到搜索结果末尾")
                    # 但仍然继续搜索剩余页面
            else:
                failed_pages = 0  # 重置失败计数器
                all_articles.extend(articles)
                print(f"第 {page} 页找到 {len(articles)} 篇文章")

            # 为了避免请求过于频繁，添加延时
            time.sleep(1.5)  # 增加延时到1.5秒，避免被反爬

        print(f"强制搜索完成！总共在{max_pages}页中找到 {len(all_articles)} 篇文章")

        # 在所有搜索完成后，统一过滤最近的文章
        if all_articles:
            recent_articles = self.filter_recent_articles(all_articles, hours)
            print(f"其中 {len(recent_articles)} 篇是最近{hours}小时内发布的")

        return recent_articles

    def search_recent_articles(self, keyword: str, hours: int = 48, max_pages: int = 10) -> List[Dict]:
        """
        搜索最近指定小时内的文章（保持向后兼容）
        现在默认使用强制搜索模式

        Args:
            keyword: 搜索关键词
            hours: 时间范围（小时），默认48小时
            max_pages: 最大搜索页数

        Returns:
            List[Dict]: 最近文章列表
        """
        return self.search_recent_articles_force(keyword, hours, max_pages)

    def print_articles(self, articles: List[Dict]) -> None:
        """
        打印文章信息

        Args:
            articles: 文章信息列表
        """
        if not articles:
            print("未找到相关文章")
            return

        print(f"\n找到 {len(articles)} 篇文章:")
        print("=" * 80)

        for i, article in enumerate(articles, 1):
            print(f"\n{i}. {article.get('title', '无标题')}")
            print(f"   作者: {article.get('author', '未知')}")
            print(f"   时间: {article.get('publish_time', '未知')}")
            # 如果有发布时间，显示距离现在的时间。
            #
            # 为何必须做类型校验：`publish_datetime` 来自页面解析/调用方，
            # 实测可能是字符串或 None；直接参与减法会抛 TypeError，
            # 把整场“文章列表打印”打断（一篇文章脏数据埋掉整次输出）。
            pub = article.get('publish_datetime')
            if isinstance(pub, datetime):
                total_s = int((datetime.now() - pub).total_seconds())
                hours_ago = total_s // 3600
                if hours_ago < 1:
                    print(f"   发布于: {total_s // 60}分钟前")
                elif hours_ago < 24:
                    print(f"   发布于: {hours_ago}小时前")
                else:
                    print(f"   发布于: {total_s // 86400}天前")
            print(f"   摘要: {article.get('summary', '无摘要')[:100]}...")
            print(f"   链接: {article.get('url', '无链接')}")
            if article.get('image_url'):
                print(f"   图片: {article.get('image_url')}")


def _ask_int(prompt: str, default: int) -> int:
    """读取整数输入；非法输入回退到默认值（不抛异常）。

    为何不能直接 `int(input(...))`：用户输入非数字（如 `abc`、空回车）时
    会抛 ValueError 并使整个交互流程退出 —— 对命令行演示入口而言，
    应当容错而不是崩掉。

    Args:
        prompt: 提示文本
        default: 解析失败时使用的默认值

    Returns:
        解析出的整数，或 `default`。
    """
    raw = input(prompt).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"输入 `{raw}` 不是数字，使用默认值 {default}")
        return default


def main():
    """主函数示例"""
    searcher = SogouSearcher()

    # 搜索示例
    keyword = input("请输入搜索关键词: ").strip()
    if not keyword:
        keyword = "人工智能"  # 默认关键词

    print(f"开始搜索关键词: {keyword}")

    # 询问搜索模式
    print("\n搜索模式选择:")
    print("1. 普通搜索（单页）")
    print("2. 多页搜索")
    print("3. 最近48小时文章搜索")
    print("4. 自定义时间范围搜索")

    choice = input("请选择搜索模式 (1-4，默认3): ").strip() or "3"

    if choice == "1":
        # 搜索单页
        articles = searcher.search_articles(keyword)
        searcher.print_articles(articles)
    elif choice == "2":
        # 搜索多页
        max_pages = _ask_int("请输入最大搜索页数 (默认3): ", 3)
        all_articles = searcher.search_with_pagination(keyword, max_pages)
        searcher.print_articles(all_articles)
        print(f"\n总共找到 {len(all_articles)} 篇文章")
    elif choice == "3":
        # 搜索最近48小时文章
        recent_articles = searcher.search_recent_articles(keyword, 48)
        searcher.print_articles(recent_articles)
    elif choice == "4":
        # 自定义时间范围搜索
        hours = _ask_int("请输入时间范围（小时，默认48）: ", 48)
        max_pages = _ask_int("请输入最大搜索页数 (默认10): ", 10)
        recent_articles = searcher.search_recent_articles(keyword, hours, max_pages)
        searcher.print_articles(recent_articles)
    else:
        print("无效选择，默认搜索最近48小时文章")
        recent_articles = searcher.search_recent_articles(keyword, 48)
        searcher.print_articles(recent_articles)


if __name__ == "__main__":
    main()