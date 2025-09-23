# -*- coding: utf-8 -*-
import requests
import json
import time
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import re
import os

class QiumiwuCrawler:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1'
        })
        self.base_url = 'https://www.qiumiwu.com'
        self.api_base_url = 'https://api.qiumiwu.com'
        
    def parse_time(self, time_str):
        """解析时间字符串"""
        now = datetime.now()
        
        if '小时前' in time_str:
            hours = int(re.search(r'(\d+)', time_str).group(1))
            return now - timedelta(hours=hours)
        elif '分钟前' in time_str:
            minutes = int(re.search(r'(\d+)', time_str).group(1))
            return now - timedelta(minutes=minutes)
        elif '天前' in time_str:
            days = int(re.search(r'(\d+)', time_str).group(1))
            return now - timedelta(days=days)
        elif '昨天' in time_str:
            return now - timedelta(days=1)
        elif '前天' in time_str:
            return now - timedelta(days=2)
        else:
            try:
                return datetime.strptime(time_str, '%Y-%m-%d %H:%M')
            except:
                return now
    
    def is_within_48_hours(self, publish_time):
        """检查是否在48小时内"""
        if isinstance(publish_time, str):
            publish_time = self.parse_time(publish_time)
        
        now = datetime.now()
        return (now - publish_time).total_seconds() <= 48 * 3600
    
    def get_initial_articles(self):
        """从初始HTML页面获取文章列表"""
        try:
            response = self.session.get(f'{self.base_url}/news/yuce', timeout=30)
            response.raise_for_status()
            response.encoding = 'utf-8'
            
            soup = BeautifulSoup(response.text, 'html.parser')
            articles = []
            
            # 从HTML中解析文章
            article_elements = soup.find_all('div', class_='news-item') or soup.find_all('a', href=re.compile(r'/news/\d+'))
            
            for i, element in enumerate(article_elements):
                try:
                    # 提取文章链接
                    link_elem = element if element.name == 'a' else element.find('a', href=re.compile(r'/news/\d+'))
                    if not link_elem:
                        continue
                    
                    article_url = link_elem.get('href')
                    if not article_url.startswith('http'):
                        if article_url.startswith('//'):
                            article_url = 'https:' + article_url
                        elif article_url.startswith('/'):
                            article_url = self.base_url + article_url
                        else:
                            article_url = self.base_url + '/' + article_url
                    
                    # 提取文章ID
                    article_id = re.search(r'/news/(\d+)', article_url)
                    if not article_id:
                        continue
                    article_id = article_id.group(1)
                    
                    # 提取标题
                    title_elem = element.find('h3') or element.find('h2') or element.find(class_=re.compile(r'title'))
                    title = title_elem.get_text(strip=True) if title_elem else link_elem.get_text(strip=True)
                    
                    # 提取时间
                    time_elem = element.find(class_=re.compile(r'time|date')) or element.find('span', string=re.compile(r'小时前|分钟前|天前'))
                    publish_time = time_elem.get_text(strip=True) if time_elem else None
                    
                    if title and article_id:
                        articles.append({
                            'id': article_id,
                            'title': title,
                            'url': article_url,
                            'publish_time': publish_time
                        })
                        
                except Exception as e:
                    print(f"解析文章元素时出错: {e}")
                    continue
            
            print(f"从初始页面获取到 {len(articles)} 篇文章")
            return articles
            
        except Exception as e:
            print(f"获取初始文章列表失败: {e}")
            return []
    
    def get_api_articles(self, last_article_id=None, page=1):
        """通过API获取文章列表"""
        try:
            # 如果没有last_article_id，使用当前时间戳
            if not last_article_id:
                last_article_id = str(int(time.time() * 1000))
            
            api_url = f'{self.api_base_url}/news/list/1/16/{last_article_id}/0/0'
            
            response = self.session.get(api_url, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            articles = []
            
            # API响应结构是 data.data.list 而不是 data.data
            if data.get('error') == 0 and data.get('data') and data['data'].get('list'):
                for item in data['data']['list']:
                    try:
                        article_id = item.get('id')
                        title = item.get('title')
                        path = item.get('path')
                        publish_time = item.get('publishTime')
                        
                        if article_id and title and path:
                            article_url = self.base_url + path
                            
                            articles.append({
                                'id': article_id,
                                'title': title,
                                'url': article_url,
                                'publish_time': publish_time,
                                'author': item.get('author', {}),
                                'cover': item.get('cover', []),
                                'summary': item.get('summary', ''),
                                'league': item.get('league')
                            })
                    except Exception as e:
                        print(f"解析API文章数据时出错: {e}")
                        continue
            
            print(f"从API获取到 {len(articles)} 篇文章")
            return articles
            
        except Exception as e:
            print(f"API请求失败: {e}")
            return []
    
    def get_article_content(self, article_url):
        """获取文章详细内容"""
        try:
            response = self.session.get(article_url, timeout=30)
            response.raise_for_status()
            response.encoding = 'utf-8'
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 查找文章内容容器
            content_selectors = [
                '.article-content',
                '.news-content',
                '.content',
                '#content',
                '.detail-content',
                'article',
                '.main-content'
            ]
            
            content = ""
            for selector in content_selectors:
                content_elem = soup.select_one(selector)
                if content_elem:
                    # 移除广告和无关元素
                    for unwanted in content_elem.find_all(['script', 'style', 'iframe', 'ins']):
                        unwanted.decompose()
                    
                    content = content_elem.get_text(separator='\n', strip=True)
                    break
            
            # 如果找不到专门的内容容器，尝试从整个页面提取
            if not content:
                main_elem = soup.find('main') or soup.find(id='main') or soup.find(class_='main')
                if main_elem:
                    content = main_elem.get_text(separator='\n', strip=True)
            
            return content
            
        except Exception as e:
            print(f"获取文章内容失败 {article_url}: {e}")
            return ""
    
    def crawl_articles(self, max_pages=5):
        """爬取文章，包含分页"""
        all_articles = []
        
        # 首先获取初始页面的文章
        print("正在获取初始页面文章...")
        initial_articles = self.get_initial_articles()
        
        # 过滤48小时内的文章
        valid_articles = []
        for article in initial_articles:
            if article.get('publish_time') and self.is_within_48_hours(article['publish_time']):
                valid_articles.append(article)
        
        all_articles.extend(valid_articles)
        print(f"初始页面48小时内文章: {len(valid_articles)} 篇")
        
        # 通过API获取更多文章
        last_article_id = None
        if valid_articles:
            last_article_id = valid_articles[-1]['id']
        
        for page in range(1, max_pages + 1):
            print(f"正在获取第 {page} 页API文章...")
            api_articles = self.get_api_articles(last_article_id, page)
            
            if not api_articles:
                print("没有更多文章，停止翻页")
                break
            
            # 过滤48小时内的文章
            valid_api_articles = []
            for article in api_articles:
                if article.get('publish_time') and self.is_within_48_hours(article['publish_time']):
                    valid_api_articles.append(article)
                else:
                    # 如果文章超过48小时，停止翻页
                    if article.get('publish_time'):
                        print(f"发现超过48小时的文章，停止翻页")
                        return all_articles
            
            all_articles.extend(valid_api_articles)
            print(f"第 {page} 页48小时内文章: {len(valid_api_articles)} 篇")
            
            # 更新last_article_id用于下一页
            if valid_api_articles:
                last_article_id = valid_api_articles[-1]['id']
            
            # 添加延时避免请求过快
            time.sleep(1)
        
        return all_articles
    
    def get_complete_articles(self, max_pages=5, save_to_file=True):
        """获取完整的文章内容"""
        print("开始爬取球迷屋文章...")
        
        # 获取文章列表
        articles = self.crawl_articles(max_pages)
        print(f"总共找到 {len(articles)} 篇48小时内的文章")
        
        # 获取每篇文章的完整内容
        complete_articles = []
        for i, article in enumerate(articles, 1):
            print(f"正在获取第 {i}/{len(articles)} 篇文章内容: {article['title'][:50]}...")
            
            content = self.get_article_content(article['url'])
            article['content'] = content
            article['content_length'] = len(content)
            
            complete_articles.append(article)
            
            # 添加延时避免请求过快
            time.sleep(2)
        
        if save_to_file:
            self.save_articles(complete_articles)
        
        return complete_articles
    
    def save_articles(self, articles):
        """保存文章到文件"""
        timestamp = int(time.time())
        filename = f"qiumiwu_articles_{timestamp}.json"
        
        # 确保输出目录存在
        os.makedirs("output", exist_ok=True)
        filepath = os.path.join("output", filename)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(articles, f, ensure_ascii=False, indent=2)
        
        print(f"文章已保存到: {filepath}")
        print(f"总计 {len(articles)} 篇文章")

def main():
    crawler = QiumiwuCrawler()
    
    try:
        # 获取完整的文章内容
        articles = crawler.get_complete_articles(max_pages=100)
        
        print("\n=== 爬取完成 ===")
        print(f"成功获取 {len(articles)} 篇文章")
        
        # 显示文章摘要
        for i, article in enumerate(articles[:5], 1):
            print(f"\n{i}. {article['title']}")
            print(f"   发布时间: {article.get('publish_time', 'Unknown')}")
            print(f"   内容长度: {article.get('content_length', 0)} 字符")
            print(f"   URL: {article['url']}")
        
        if len(articles) > 5:
            print(f"\n... 还有 {len(articles) - 5} 篇文章")
            
    except KeyboardInterrupt:
        print("\n用户中断爬取")
    except Exception as e:
        print(f"爬取过程中出错: {e}")

if __name__ == "__main__":
    main()