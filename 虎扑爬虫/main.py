# -*- coding: utf-8 -*-
import requests
from bs4 import BeautifulSoup
import json
import re
import time
from urllib.parse import quote
from datetime import datetime, timedelta
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class HupuCrawler:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
        })
    
    def search_articles_auto_pagination(self, query, hours_limit=48, max_pages=20):
        """
        自动翻页搜索文章，直到找到超过指定小时数的文章
        
        Args:
            query: 搜索关键词
            hours_limit: 时间限制（小时），默认48小时
            max_pages: 最大页数限制，防止无限翻页
        
        Returns:
            dict: 包含所有符合时间要求的文章列表
        """
        all_articles = []
        current_page = 1
        cutoff_time = datetime.now() - timedelta(hours=hours_limit)
        
        while current_page <= max_pages:
            logging.info(f"正在搜索第{current_page}页...")
            result = self.search_articles(query, page=current_page)
            
            if not result['success']:
                logging.error(f"第{current_page}页搜索失败: {result.get('error')}")
                break
            
            articles = result.get('articles', [])
            if not articles:
                logging.info("没有更多文章了")
                break
            
            # 检查文章时间并过滤
            page_has_recent = False
            for article in articles:
                article_time = self._parse_article_time(article.get('publish_time', ''))
                if article_time and article_time >= cutoff_time:
                    all_articles.append(article)
                    page_has_recent = True
                elif article_time:
                    # 如果文章时间早于限制时间，说明后续文章也会更早
                    logging.info(f"发现超过{hours_limit}小时的文章: {article['title'][:30]}...")
                    break
            
            # 如果这一页没有符合时间要求的文章，停止搜索
            if not page_has_recent:
                logging.info(f"第{current_page}页没有{hours_limit}小时内的文章，停止搜索")
                break
            
            current_page += 1
            
            # 避免请求过快
            time.sleep(1)
        
        return {
            'success': True,
            'query': query,
            'total_articles': len(all_articles),
            'hours_limit': hours_limit,
            'pages_searched': current_page - 1,
            'articles': all_articles
        }
    
    def _parse_article_time(self, time_str):
        """
        解析文章发布时间
        支持格式: "2025-09-23", "5分钟前", "1小时前", "3天前" 等
        """
        if not time_str:
            return None
        
        now = datetime.now()
        time_str = time_str.strip()
        
        try:
            # 处理相对时间
            if '分钟前' in time_str:
                minutes = int(re.search(r'(\d+)分钟前', time_str).group(1))
                return now - timedelta(minutes=minutes)
            elif '小时前' in time_str:
                hours = int(re.search(r'(\d+)小时前', time_str).group(1))
                return now - timedelta(hours=hours)
            elif '天前' in time_str:
                days = int(re.search(r'(\d+)天前', time_str).group(1))
                return now - timedelta(days=days)
            elif '昨天' in time_str:
                return now - timedelta(days=1)
            elif '前天' in time_str:
                return now - timedelta(days=2)
            # 处理绝对时间格式 YYYY-MM-DD
            elif re.match(r'\d{4}-\d{2}-\d{2}', time_str):
                return datetime.strptime(time_str, '%Y-%m-%d')
            else:
                # 尝试其他可能的日期格式
                for fmt in ['%m-%d', '%Y/%m/%d', '%m/%d']:
                    try:
                        parsed_time = datetime.strptime(time_str, fmt)
                        # 如果没有年份，假设是当前年份
                        if parsed_time.year == 1900:
                            parsed_time = parsed_time.replace(year=now.year)
                        return parsed_time
                    except:
                        continue
        except:
            pass
        
        logging.warning(f"无法解析时间格式: {time_str}")
        return None
    
    def search_articles(self, query, page=1, sort_by='createtime'):
        """
        搜索虎扑文章
        
        Args:
            query: 搜索关键词
            page: 页码，默认1
            sort_by: 排序方式，默认按发布时间最新排序
                - createtime: 按发布时间最新排序
                - createtime_asc: 按发布时间最早排序
                - replytime: 按回复时间排序
                - lights: 按亮回复数排序(近1月)
                - replies: 按回复数排序(近1月)
        
        Returns:
            dict: 包含文章列表和搜索信息
        """
        url = "https://bbs.hupu.com/search"
        params = {
            'q': query,
            'topicId': '',
            'sortby': sort_by,
            'page': page
        }
        
        try:
            logging.info(f"正在搜索: {query}, 第{page}页")
            response = self.session.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            # 解析HTML
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 提取JSON数据
            json_data = self._extract_json_data(response.text)
            if json_data:
                return self._parse_json_data(json_data, query)
            
            # 如果JSON解析失败，使用HTML解析
            return self._parse_html_data(soup, query)
            
        except requests.RequestException as e:
            logging.error(f"请求失败: {e}")
            return {'success': False, 'error': str(e)}
        except Exception as e:
            logging.error(f"解析失败: {e}")
            return {'success': False, 'error': str(e)}
    
    def _extract_json_data(self, html):
        """从HTML中提取JSON数据"""
        try:
            # 尝试多种可能的JSON数据模式
            patterns = [
                r'window\.\$\$data\s*=\s*({.*?});',
                r'window\.initialProps\s*=\s*({.*?});',
                r'window\.pageData\s*=\s*({.*?});'
            ]
            
            for pattern in patterns:
                match = re.search(pattern, html, re.DOTALL)
                if match:
                    try:
                        return json.loads(match.group(1))
                    except json.JSONDecodeError as e:
                        logging.warning(f"JSON解析失败，尝试下一个模式: {e}")
                        continue
                        
        except Exception as e:
            logging.warning(f"提取JSON数据失败: {e}")
        return None
    
    def _parse_json_data(self, data, query):
        """解析JSON数据格式的文章列表"""
        try:
            search_res = data.get('searchRes', {})
            articles = []
            
            for item in search_res.get('data', []):
                article = {
                    'id': item.get('id'),
                    'title': self._clean_html_tags(item.get('title', '')),
                    'url': f"https://bbs.hupu.com/{item.get('id')}.html",
                    'author': item.get('username', ''),
                    'author_avatar': item.get('header', ''),
                    'forum_name': item.get('forum_name', ''),
                    'replies': int(item.get('replies', 0)),
                    'lights': int(item.get('lights', 0)),
                    'recommendations': int(item.get('recNum', 0)),
                    'publish_time': item.get('addTimeDisplay', ''),
                    'content_preview': self._clean_html_tags(item.get('content', ''))[:200],
                    'is_news': item.get('isNews') == '1',
                    'has_picture': item.get('isPic') == '1',
                    'picture_url': item.get('picture', '') if item.get('isPic') == '1' else None
                }
                articles.append(article)
            
            return {
                'success': True,
                'query': query,
                'total_count': search_res.get('count', 0),
                'total_pages': search_res.get('totalPage', 0),
                'current_page': int(data.get('query', {}).get('page', 1)),
                'articles': articles,
                'has_next_page': search_res.get('hasNextPage', 0) == 1
            }
            
        except Exception as e:
            logging.error(f"JSON数据解析失败: {e}")
            return {'success': False, 'error': str(e)}
    
    def _parse_html_data(self, soup, query):
        """解析HTML格式的文章列表"""
        try:
            articles = []
            content_outlines = soup.find_all('div', class_='content-outline')
            
            for outline in content_outlines:
                content_wrap = outline.find('div', class_='content-wrap')
                if not content_wrap:
                    continue
                
                # 提取标题和链接
                title_link = content_wrap.find('a', class_='content-wrap-span')
                if not title_link:
                    continue
                
                title = self._clean_html_tags(title_link.text.strip())
                url = title_link.get('href', '')
                if url and not url.startswith('http'):
                    url = 'https://bbs.hupu.com' + url if not url.startswith('/') else 'https://bbs.hupu.com' + url
                
                # 提取专区信息
                forum_links = content_wrap.find_all('a', class_='content-wrap-span')
                forum_name = forum_links[1].text.strip() if len(forum_links) > 1 else ''
                
                # 提取时间和统计数据
                spans = content_wrap.find_all('span')
                publish_time = ''
                replies = 0
                recommendations = 0
                lights = 0
                
                for span in spans:
                    text = span.text.strip()
                    if '-' in text and len(text) == 10:  # 日期格式
                        publish_time = text
                    elif span.get('class') == ['content-wrap-span1'] and text.isdigit():
                        if not replies:
                            replies = int(text)
                        elif not recommendations:
                            recommendations = int(text)
                        elif not lights:
                            lights = int(text)
                
                article = {
                    'title': title,
                    'url': url,
                    'forum_name': forum_name,
                    'publish_time': publish_time,
                    'replies': replies,
                    'recommendations': recommendations,
                    'lights': lights
                }
                articles.append(article)
            
            return {
                'success': True,
                'query': query,
                'articles': articles,
                'source': 'html_parsing'
            }
            
        except Exception as e:
            logging.error(f"HTML解析失败: {e}")
            return {'success': False, 'error': str(e)}
    
    def _clean_html_tags(self, text):
        """清理HTML标签"""
        if not text:
            return ''
        # 移除HTML标签
        clean = re.sub(r'<[^>]+>', '', text)
        # 移除多余空白
        clean = re.sub(r'\s+', ' ', clean).strip()
        return clean
    
    def get_article_detail(self, article_url):
        """获取文章详细内容"""
        try:
            response = self.session.get(article_url, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 尝试从JSON数据中获取信息
            json_data = self._extract_next_data(response.text)
            if json_data:
                return self._parse_article_json_data(json_data, article_url)
            
            # 如果JSON解析失败，使用HTML解析
            return self._parse_article_html_data(soup, article_url)
            
        except Exception as e:
            logging.error(f"获取文章详情失败: {e}")
            return {'success': False, 'error': str(e)}
    
    def _extract_next_data(self, html):
        """从HTML中提取__NEXT_DATA__的JSON数据"""
        try:
            # 支持多种Next.js数据提取模式
            patterns = [
                r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                r'window\.__NEXT_DATA__\s*=\s*(.*?);',
                r'__NEXT_DATA__\s*=\s*(.*?);'
            ]
            
            for pattern in patterns:
                match = re.search(pattern, html, re.DOTALL)
                if match:
                    try:
                        json_str = match.group(1).strip()
                        return json.loads(json_str)
                    except json.JSONDecodeError as e:
                        logging.warning(f"Next.js JSON解析失败，尝试下一个模式: {e}")
                        continue
                        
        except Exception as e:
            logging.warning(f"解析__NEXT_DATA__失败: {e}")
        return None
    
    def _parse_article_json_data(self, data, article_url):
        """解析JSON数据格式的文章详情"""
        try:
            props = data.get('props', {})
            page_props = props.get('pageProps', {})
            detail = page_props.get('detail', {})
            thread = detail.get('thread', {})
            
            # 提取基本信息
            article_detail = {
                'success': True,
                'url': article_url,
                'id': thread.get('tid', ''),
                'title': thread.get('title', ''),
                'content': self._extract_content_from_json(thread.get('content', '')),
                'plain_content': self._clean_html_tags(thread.get('content', '')),
                'author': {
                    'id': thread.get('authorId', ''),
                    'name': thread.get('author', {}).get('puname', ''),
                    'level': thread.get('author', {}).get('level', 0),
                    'avatar': thread.get('author', {}).get('header', ''),
                    'profile_url': thread.get('author', {}).get('url', '')
                },
                'statistics': {
                    'replies': thread.get('replies', 0),
                    'lights': thread.get('lights', 0),
                    'recommendations': thread.get('recommend', 0),
                    'views': thread.get('read', 0)
                },
                'publish_time': thread.get('createdAtFormat', ''),
                'publish_timestamp': thread.get('createdAt', 0),
                'topic': {
                    'id': thread.get('topicId', ''),
                    'name': thread.get('topic', {}).get('name', ''),
                    'url': thread.get('topic', {}).get('url', '')
                },
                'breadcrumb': detail.get('breadCrumb', []),
                'has_video': thread.get('hasVideo', False),
                'video_cover': thread.get('videoCover', ''),
                'images': self._extract_images_from_json(thread)
            }
            
            return article_detail
            
        except Exception as e:
            logging.error(f"JSON文章数据解析失败: {e}")
            return {'success': False, 'error': str(e)}
    
    def _parse_article_html_data(self, soup, article_url):
        """解析HTML格式的文章详情"""
        try:
            # 提取标题
            title_elem = soup.find('h1', class_='index_name__M5qqs')
            title = title_elem.text.strip() if title_elem else ''
            
            # 提取作者信息
            author_name_elem = soup.find('a', class_='post-user_post-user-comp-info-top-name__N3D4w')
            author_name = author_name_elem.text.strip() if author_name_elem else ''
            author_url = author_name_elem.get('href', '') if author_name_elem else ''
            
            # 提取统计信息
            stats = {'replies': 0, 'lights': 0, 'views': 0}
            reply_elem = soup.find('span', class_='index_reply__GP3PX')
            if reply_elem:
                stats['replies'] = int(re.search(r'(\d+)', reply_elem.text).group(1) if re.search(r'(\d+)', reply_elem.text) else 0)
            
            light_elem = soup.find('span', class_='index_light__M2WPs')
            if light_elem:
                light_match = re.search(r'(\d+)', light_elem.text)
                stats['lights'] = int(light_match.group(1) if light_match else 0)
            
            read_elem = soup.find('span', class_='index_read__7h1Dm')
            if read_elem:
                read_match = re.search(r'(\d+)', read_elem.text)
                stats['views'] = int(read_match.group(1) if read_match else 0)
            
            # 提取发布时间
            time_elem = soup.find('span', class_='post-user_post-user-comp-info-top-time__k9K2U')
            publish_time = time_elem.text.strip() if time_elem else ''
            
            # 提取文章内容
            content_elem = soup.find('div', class_='thread-content-detail')
            content_html = str(content_elem) if content_elem else ''
            content_text = content_elem.get_text(separator='\n', strip=True) if content_elem else ''
            
            # 提取专区信息
            topic_elem = soup.find('a', class_='post-user_post-user-comp-info-bottom-link__BMF8U')
            topic_name = topic_elem.text.strip() if topic_elem else ''
            topic_url = topic_elem.get('href', '') if topic_elem else ''
            
            article_detail = {
                'success': True,
                'url': article_url,
                'title': title,
                'content': content_html,
                'plain_content': content_text,
                'author': {
                    'name': author_name,
                    'profile_url': author_url
                },
                'statistics': stats,
                'publish_time': publish_time,
                'topic': {
                    'name': topic_name,
                    'url': topic_url
                },
                'source': 'html_parsing'
            }
            
            return article_detail
            
        except Exception as e:
            logging.error(f"HTML文章解析失败: {e}")
            return {'success': False, 'error': str(e)}
    
    def _extract_content_from_json(self, content_html):
        """从JSON中的HTML内容提取纯文本"""
        if not content_html:
            return ''
        
        # 使用BeautifulSoup清理HTML标签，保留基本格式
        soup = BeautifulSoup(content_html, 'html.parser')
        
        # 替换一些HTML标签为文本格式
        for br in soup.find_all('br'):
            br.replace_with('\n')
        
        for p in soup.find_all('p'):
            p.insert_after('\n')
        
        text = soup.get_text()
        # 清理多余的空行
        text = re.sub(r'\n\s*\n', '\n\n', text)
        
        return text.strip()
    
    def _extract_images_from_json(self, thread_data):
        """从JSON数据中提取图片信息"""
        images = []
        
        # 从format字段中提取图片
        format_data = thread_data.get('format', '')
        if format_data:
            try:
                format_json = json.loads(format_data)
                img_list = format_json.get('imgList', [])
                for img in img_list:
                    images.append({
                        'url': img.get('remoteUrl', ''),
                        'key': img.get('key', '')
                    })
            except:
                pass
        
        return images

    def save_articles_with_content_to_file(self, articles, filename=None):
        """将文章数据及其完整内容保存到JSON文件"""
        if not filename:
            timestamp = int(time.time())
            filename = f"hupu_articles_with_content_{timestamp}.json"
        
        try:
            articles_with_content = []
            
            for i, article in enumerate(articles, 1):
                logging.info(f"正在获取第{i}篇文章的完整内容...")
                
                # 获取文章详细内容
                if article.get('url'):
                    detail_result = self.get_article_detail(article['url'])
                    if detail_result.get('success'):
                        # 合并基础信息和详细内容
                        full_article = {**article, **detail_result}
                        articles_with_content.append(full_article)
                    else:
                        # 如果获取详细内容失败，保留基础信息
                        article['content_error'] = detail_result.get('error', '获取内容失败')
                        articles_with_content.append(article)
                else:
                    articles_with_content.append(article)
                
                # 添加延时避免请求过快
                if i < len(articles):
                    time.sleep(2)
            
            # 保存到文件
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(articles_with_content, f, ensure_ascii=False, indent=2)
            
            logging.info(f"包含完整内容的文章数据已保存到: {filename}")
            return filename
            
        except Exception as e:
            logging.error(f"保存文件失败: {e}")
            return None

    def load_articles_from_file(self, filename):
        """从JSON文件加载文章数据"""
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"加载文件失败: {e}")
            return None

def main():
    crawler = HupuCrawler()
    
    # 测试自动翻页搜索48小时内的文章
    query = "大阪钢巴VS横滨水手"
    result = crawler.search_articles_auto_pagination(query, hours_limit=48)
    
    if result['success']:
        print(f"搜索结果: {query}")
        print(f"时间限制: {result['hours_limit']}小时内")
        print(f"搜索页数: {result['pages_searched']}页")
        print(f"找到文章: {result['total_articles']}篇")
        print("-" * 50)
        
        # 按时间排序显示最新的文章
        sorted_articles = sorted(result['articles'], 
                                key=lambda x: crawler._parse_article_time(x.get('publish_time', '')) or datetime.min, 
                                reverse=True)
        
        for i, article in enumerate(sorted_articles[:10], 1):
            print(f"{i}. {article['title']}")
            print(f"   专区: {article.get('forum_name', 'N/A')}")
            print(f"   时间: {article.get('publish_time', 'N/A')}")
            print(f"   回复: {article.get('replies', 0)}, 推荐: {article.get('recommendations', 0)}")
            print(f"   链接: {article.get('url', 'N/A')}")
            print()
        
        # 获取所有文章的完整内容
        if sorted_articles:
            print("\n" + "=" * 80)
            print("获取所有文章的完整内容:")
            print("=" * 80)
            
            for i, article in enumerate(sorted_articles, 1):
                print(f"\n{'='*20} 第{i}篇文章 {'='*20}")
                print(f"标题: {article['title']}")
                print(f"链接: {article.get('url', 'N/A')}")
                print(f"时间: {article.get('publish_time', 'N/A')}")
                print("-" * 60)
                
                detail_result = crawler.get_article_detail(article['url'])
                
                if detail_result.get('success'):
                    print(f"作者: {detail_result.get('author', {}).get('name', 'N/A')}")
                    print(f"专区: {detail_result.get('topic', {}).get('name', 'N/A')}")
                    
                    stats = detail_result.get('statistics', {})
                    print(f"统计: 回复 {stats.get('replies', 0)}, 亮 {stats.get('lights', 0)}, 阅读 {stats.get('views', 0)}")
                    
                    images = detail_result.get('images', [])
                    if images:
                        print(f"图片数量: {len(images)}")
                        for j, img in enumerate(images[:3], 1):  # 只显示前3张图片链接
                            print(f"  图片{j}: {img.get('url', 'N/A')}")
                        if len(images) > 3:
                            print(f"  ... 还有{len(images) - 3}张图片")
                    
                    print("\n文章完整内容:")
                    print("-" * 50)
                    content = detail_result.get('plain_content', '')
                    if content:
                        print(content)
                    else:
                        # 如果plain_content为空，尝试使用content字段
                        html_content = detail_result.get('content', '')
                        if html_content:
                            print("HTML格式内容:")
                            print(html_content)
                        else:
                            print("无法获取文章内容")
                    print("-" * 50)
                        
                else:
                    print(f"获取文章详情失败: {detail_result.get('error', '未知错误')}")
                
                # 在每篇文章之间添加延时，避免请求过快
                if i < len(sorted_articles):
                    print(f"\n等待2秒后获取下一篇文章...")
                    time.sleep(2)
        
        # 保存搜索结果到文件
        print("\n" + "-" * 50)
        print("保存数据到文件中...")
        
        # 保存基础搜索结果
        basic_filename = f"hupu_search_basic_{int(time.time())}.json"
        with open(basic_filename, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"基础搜索结果已保存到: {basic_filename}")
        
        # 保存包含完整内容的文章数据
        if sorted_articles:
            print("正在保存包含完整内容的文章数据...")
            full_content_filename = crawler.save_articles_with_content_to_file(sorted_articles)
            if full_content_filename:
                print(f"包含完整内容的文章数据已保存到: {full_content_filename}")
            else:
                print("保存完整内容文件失败")
    else:
        print(f"搜索失败: {result.get('error', '未知错误')}")

if __name__ == "__main__":
    main()