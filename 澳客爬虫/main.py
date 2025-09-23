# -*- coding: utf-8 -*-
import requests
import json
import time
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import re
import os

class AokeCrawler:
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
        
        # 设置必要的cookies
        cookies = {
            'PHPSESSID': '467800936f69c300e181cab43a07d3ed0cd97b5f',
            'LastUrl': '',
            'FirstURL': 'wap.okooo.com/',
            'FirstOKURL': 'https%3A//www.okooo.com/jingcai/',
            'First_Source': 'wap.okooo.com',
            'pm': '',
            'LStatus': 'N',
            'LoginStr': '%7B%22welcome%22%3A%22%u60A8%u597D%uFF0C%u6B22%u8FCE%u60A8%22%2C%22login%22%3A%22%u767B%u5F55%22%2C%22register%22%3A%22%u6CE8%u518C%22%2C%22TrustLoginArr%22%3A%7B%22alipay%22%3A%7B%22LoginCn%22%3A%22%22%7D%2C%22tenpay%22%3A%7B%22LoginCn%22%3A%22%u8D22%u4ED8%u901A%22%7D%2C%22weibo%22%3A%7B%22LoginCn%22%3A%22%u65B0%u6D6A%u5FAE%u535A%22%7D%2C%22renren%22%3A%7B%22LoginCn%22%3A%22%22%7D%2C%22baidu%22%3A%7B%22LoginCn%22%3A%22%22%7D%2C%22snda%22%3A%7B%22LoginCn%22%3A%22%22%7D%7D%2C%22userlevel%22%3A%22%22%2C%22flog%22%3A%22hidden%22%2C%22UserInfo%22%3A%22%22%2C%22loginSession%22%3A%22___GlobalSession%22%7D',
            'acw_tc': '0aef342417586210138235011e571b3c65cd391703fa9e32e9cce8fb7f4f5b'
        }
        
        for name, value in cookies.items():
            self.session.cookies.set(name, value)
        
        self.base_url = 'https://zx.okooo.com'
        
    def parse_time(self, time_str):
        """解析澳客的时间格式"""
        now = datetime.now()
        
        if '刚刚' in time_str:
            return now
        elif '分钟前' in time_str:
            minutes = int(re.search(r'(\d+)', time_str).group(1))
            return now - timedelta(minutes=minutes)
        elif '小时前' in time_str:
            hours = int(re.search(r'(\d+)', time_str).group(1))
            return now - timedelta(hours=hours)
        elif '天前' in time_str:
            days = int(re.search(r'(\d+)', time_str).group(1))
            return now - timedelta(days=days)
        elif ':' in time_str and len(time_str) <= 6:
            # 处理 "16:49" 格式，默认为今天
            try:
                hour, minute = map(int, time_str.split(':'))
                today = now.date()
                return datetime.combine(today, datetime.min.time().replace(hour=hour, minute=minute))
            except:
                return now
        else:
            return now
    
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
            
            # 查找文章列表容器
            news_list = soup.find('div', id='mathNewList')
            if not news_list:
                print(f"未找到文章列表容器，页面: {page_url}")
                return []
            
            # 遍历每个文章项
            for item in news_list.find_all('div', class_='news_left_item'):
                try:
                    # 提取文章标题和链接
                    title_elem = item.find('h2', class_='news_title')
                    if not title_elem:
                        continue
                    
                    link_elem = title_elem.find('a')
                    if not link_elem:
                        continue
                    
                    title = link_elem.get_text(strip=True)
                    article_url = link_elem.get('href')
                    
                    # 处理相对链接
                    if article_url.startswith('/'):
                        article_url = self.base_url + article_url
                    
                    # 提取文章ID
                    article_id_match = re.search(r'/news/([^/]+)/', article_url)
                    if not article_id_match:
                        continue
                    article_id = article_id_match.group(1)
                    
                    # 提取摘要
                    summary_elem = item.find('p', class_='summary')
                    summary = ""
                    if summary_elem:
                        summary_link = summary_elem.find('a')
                        if summary_link:
                            summary = summary_link.get_text(strip=True)
                    
                    # 提取时间
                    time_elem = item.find('span', class_='cyq_newope_time')
                    publish_time = time_elem.get_text(strip=True) if time_elem else ""
                    
                    # 提取图片
                    img_elem = item.find('a', class_='img_box')
                    cover_img = ""
                    if img_elem:
                        img_tag = img_elem.find('img')
                        if img_tag:
                            cover_img = img_tag.get('src', '')
                    
                    if title and article_id:
                        articles.append({
                            'id': article_id,
                            'title': title,
                            'url': article_url,
                            'summary': summary,
                            'publish_time': publish_time,
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
            
            # 澳客网站的文章内容结构
            content_selectors = [
                '.article-content',
                '.news-content',
                '.content',
                '.detail-content',
                '.newsDetail',
                'article',
                '.main-content'
            ]
            
            content = ""
            for selector in content_selectors:
                content_elem = soup.select_one(selector)
                if content_elem:
                    # 移除广告和无关元素
                    for unwanted in content_elem.find_all(['script', 'style', 'iframe', 'ins', 'noscript']):
                        unwanted.decompose()
                    
                    content = content_elem.get_text(separator='\n', strip=True)
                    break
            
            # 如果找不到专门的内容容器，尝试其他方式
            if not content:
                # 查找可能的内容容器
                main_content = soup.find('main') or soup.find('article') or soup.find(id='main-content')
                if main_content:
                    content = main_content.get_text(separator='\n', strip=True)
                else:
                    # 提取body中的主要文本
                    body = soup.find('body')
                    if body:
                        # 移除导航、侧边栏等元素
                        for unwanted in body.find_all(['nav', 'footer', 'header', 'aside', 'script', 'style']):
                            unwanted.decompose()
                        content = body.get_text(separator='\n', strip=True)
            
            return content
            
        except Exception as e:
            print(f"获取文章内容失败: {e}")
            return ""
    
    def crawl_articles(self, max_pages=5):
        """爬取文章，包含分页"""
        all_articles = []
        
        for page in range(1, max_pages + 1):
            if page == 1:
                page_url = f'{self.base_url}/jingcai'
            else:
                page_url = f'{self.base_url}/jingcai/?page={page}'
            
            print(f"正在获取第 {page} 页...")
            page_articles = self.get_articles_from_page(page_url)
            
            if not page_articles:
                print(f"第 {page} 页没有获取到文章，停止翻页")
                break
            
            # 过滤48小时内的文章
            valid_articles = []
            for article in page_articles:
                if article.get('publish_time') and self.is_within_48_hours(article['publish_time']):
                    valid_articles.append(article)
                else:
                    if article.get('publish_time'):
                        print(f"发现超过48小时的文章: {article['title']}")
            
            all_articles.extend(valid_articles)
            print(f"第 {page} 页48小时内文章: {len(valid_articles)} 篇")
            
            # 如果这一页没有48小时内的文章，可能后面也没有了
            if not valid_articles:
                print("本页无48小时内文章，停止翻页")
                break
            
            # 添加延时避免请求过快
            time.sleep(2)
        
        print(f"总共获取到 {len(all_articles)} 篇48小时内的文章")
        return all_articles
    
    def get_complete_articles(self, max_pages=5, save_to_file=True):
        """获取完整的文章内容"""
        print("开始爬取澳客文章...")
        
        # 获取文章列表
        articles = self.crawl_articles(max_pages)
        print(f"总共找到 {len(articles)} 篇48小时内的文章")
        
        if not articles:
            print("没有找到符合条件的文章")
            return []
        
        # 获取每篇文章的完整内容
        complete_articles = []
        for i, article in enumerate(articles, 1):
            print(f"正在获取第 {i}/{len(articles)} 篇文章内容: {article['title'][:50]}...")
            
            content = self.get_article_content(article['url'])
            article['content'] = content
            article['content_length'] = len(content)
            
            complete_articles.append(article)
            
            # 添加延时避免请求过快
            time.sleep(3)
        
        if save_to_file:
            self.save_articles(complete_articles)
        
        return complete_articles
    
    def save_articles(self, articles):
        """保存文章到文件"""
        timestamp = int(time.time())
        filename = f"aoke_articles_{timestamp}.json"
        
        # 确保输出目录存在
        os.makedirs("output", exist_ok=True)
        filepath = os.path.join("output", filename)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(articles, f, ensure_ascii=False, indent=2)
        
        print(f"文章已保存到: {filepath}")
        print(f"总计 {len(articles)} 篇文章")

def main():
    crawler = AokeCrawler()
    
    try:
        # 获取完整的文章内容
        articles = crawler.get_complete_articles(max_pages=10)
        
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