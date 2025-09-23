# -*- coding: utf-8 -*-
import requests
import json
import time
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import re
import os

class DSCrawler:
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
        self.base_url = 'https://www.dszuqiu.com'
        
    def parse_time(self, time_str):
        """解析时间字符串，格式如 09/23 17:39"""
        try:
            if '/' in time_str and ':' in time_str:
                current_year = datetime.now().year
                time_with_year = f"{current_year}/{time_str}"
                return datetime.strptime(time_with_year, '%Y/%m/%d %H:%M')
            else:
                return datetime.now()
        except:
            return datetime.now()
    
    def is_within_48_hours(self, publish_time):
        """检查是否在48小时内"""
        if isinstance(publish_time, str):
            publish_time = self.parse_time(publish_time)
        
        now = datetime.now()
        return (now - publish_time).total_seconds() <= 48 * 3600
    
    def get_articles_from_page(self, page_url):
        """从页面获取文章列表"""
        try:
            response = self.session.get(page_url, timeout=30)
            response.raise_for_status()
            response.encoding = 'utf-8'
            
            soup = BeautifulSoup(response.text, 'html.parser')
            articles = []
            
            news_list = soup.find('ul', class_='newsListN2')
            if not news_list:
                print(f"未找到文章列表容器，页面: {page_url}")
                return []
            
            for li in news_list.find_all('li'):
                try:
                    title_link = li.find('span', class_='newsListN2Title')
                    if not title_link:
                        continue
                    
                    link_elem = title_link.find('a')
                    if not link_elem:
                        continue
                    
                    article_url = link_elem.get('href')
                    title = link_elem.get_text(strip=True)
                    
                    if article_url.startswith('/'):
                        article_url = self.base_url + article_url
                    
                    article_id_match = re.search(r'/(\d+)\.html', article_url)
                    if not article_id_match:
                        continue
                    article_id = article_id_match.group(1)
                    
                    desc_elem = li.find('p', class_='newsListN2Des')
                    description = desc_elem.get_text(strip=True) if desc_elem else ""
                    
                    time_elem = li.find('p', class_='newsListN2Time')
                    publish_time = ""
                    author = ""
                    
                    if time_elem:
                        time_text = time_elem.get_text()
                        time_match = re.search(r'(\d{2}/\d{2} \d{2}:\d{2})', time_text)
                        if time_match:
                            publish_time = time_match.group(1)
                        
                        author_elem = time_elem.find('a', class_='primary-color')
                        if author_elem:
                            author = author_elem.get_text(strip=True)
                    
                    cover_elem = li.find('a', class_='newsListN2Thum')
                    cover_img = ""
                    if cover_elem:
                        img_elem = cover_elem.find('img')
                        if img_elem:
                            cover_img = img_elem.get('src', '')
                    
                    if title and article_id:
                        articles.append({
                            'id': article_id,
                            'title': title,
                            'url': article_url,
                            'description': description,
                            'publish_time': publish_time,
                            'author': author,
                            'cover_img': cover_img
                        })
                        
                except Exception as e:
                    print(f"解析文章元素时出错: {e}")
                    continue
            
            print(f"从页面获取到 {len(articles)} 篇文章")
            return articles
            
        except Exception as e:
            print(f"获取页面失败: {e}")
            return []
    
    def get_article_content(self, article_url):
        """获取文章详细内容"""
        try:
            response = self.session.get(article_url, timeout=30)
            response.raise_for_status()
            response.encoding = 'utf-8'
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            content_selectors = [
                '.article-content',
                '.news-content', 
                '.content',
                '.newsDetail',
                '.articleContent',
                '.article-body'
            ]
            
            content = ""
            for selector in content_selectors:
                content_elem = soup.select_one(selector)
                if content_elem:
                    for unwanted in content_elem.find_all(['script', 'style', 'iframe', 'ins', 'noscript']):
                        unwanted.decompose()
                    
                    content = content_elem.get_text(separator='\n', strip=True)
                    break
            
            if not content:
                main_content = soup.find('main') or soup.find('article') or soup.find(id='main-content')
                if main_content:
                    content = main_content.get_text(separator='\n', strip=True)
            
            return content
            
        except Exception as e:
            print(f"获取文章内容失败: {e}")
            return ""
    
    def crawl_articles(self, max_pages=5):
        """爬取文章，包含分页"""
        all_articles = []
        base_url = f'{self.base_url}/articles/lottery'
        
        for page in range(1, max_pages + 1):
            if page == 1:
                page_url = base_url
            else:
                page_url = f"{base_url}/p.{page}"
            
            print(f"正在获取第 {page} 页...")
            page_articles = self.get_articles_from_page(page_url)
            
            if not page_articles:
                print(f"第 {page} 页没有获取到文章，停止翻页")
                break
            
            valid_articles = []
            for article in page_articles:
                if article.get('publish_time') and self.is_within_48_hours(article['publish_time']):
                    valid_articles.append(article)
                else:
                    if article.get('publish_time'):
                        print(f"发现超过48小时的文章: {article['title']}")
            
            all_articles.extend(valid_articles)
            print(f"第 {page} 页48小时内文章: {len(valid_articles)} 篇")
            
            if not valid_articles:
                print("本页无48小时内文章，停止翻页")
                break
            
            time.sleep(1)
        
        print(f"总共获取到 {len(all_articles)} 篇48小时内的文章")
        return all_articles
    
    def get_complete_articles(self, max_pages=5, save_to_file=True):
        """获取完整的文章内容"""
        print("开始爬取DS足球文章...")
        
        articles = self.crawl_articles(max_pages)
        print(f"总共找到 {len(articles)} 篇48小时内的文章")
        
        if not articles:
            print("没有找到符合条件的文章")
            return []
        
        complete_articles = []
        for i, article in enumerate(articles, 1):
            print(f"正在获取第 {i}/{len(articles)} 篇文章内容: {article['title'][:50]}...")
            
            content = self.get_article_content(article['url'])
            article['content'] = content
            article['content_length'] = len(content)
            
            complete_articles.append(article)
            time.sleep(2)
        
        if save_to_file:
            self.save_articles(complete_articles)
        
        return complete_articles
    
    def save_articles(self, articles):
        """保存文章到文件"""
        timestamp = int(time.time())
        filename = f"ds_articles_{timestamp}.json"
        
        os.makedirs("output", exist_ok=True)
        filepath = os.path.join("output", filename)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(articles, f, ensure_ascii=False, indent=2)
        
        print(f"文章已保存到: {filepath}")
        print(f"总计 {len(articles)} 篇文章")

def main():
    crawler = DSCrawler()
    
    try:
        articles = crawler.get_complete_articles(max_pages=100)
        
        print("\n=== 爬取完成 ===")
        print(f"成功获取 {len(articles)} 篇文章")
        
        for i, article in enumerate(articles[:5], 1):
            print(f"\n{i}. {article['title']}")
            print(f"   发布时间: {article.get('publish_time', 'Unknown')}")
            print(f"   作者: {article.get('author', 'Unknown')}")
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