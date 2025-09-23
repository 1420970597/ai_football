#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
安全的浏览器抓取服务
使用沙盒环境中的Chrome浏览器进行数据抓取
"""

import os
import json
import time
import logging
import threading
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
from contextlib import contextmanager

import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import (
    TimeoutException, WebDriverException, NoSuchElementException
)
from webdriver_manager.chrome import ChromeDriverManager
import undetected_chromedriver as uc
from fake_useragent import UserAgent
from bs4 import BeautifulSoup
import redis
from flask import Flask, request, jsonify

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class BrowserScraper:
    """安全的浏览器抓取器"""
    
    def __init__(self, redis_url: str = None):
        self.redis_client = redis.from_url(redis_url or "redis://localhost:6379/0")
        self.ua = UserAgent()
        self._driver_lock = threading.Lock()
        
        # 安全配置
        self.max_page_size = 50 * 1024 * 1024  # 50MB
        self.max_execution_time = 300  # 5分钟
        self.allowed_schemes = ['http', 'https']
        self.blocked_domains = [
            'localhost', '127.0.0.1', '0.0.0.0',
            '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'
        ]
    
    def _validate_url(self, url: str) -> bool:
        """验证URL安全性"""
        try:
            parsed = urlparse(url)
            
            # 检查协议
            if parsed.scheme not in self.allowed_schemes:
                logger.warning(f"不允许的协议: {parsed.scheme}")
                return False
            
            # 检查域名
            hostname = parsed.hostname
            if not hostname:
                return False
                
            # 检查是否为内网地址
            for blocked in self.blocked_domains:
                if hostname in blocked or hostname.startswith(blocked):
                    logger.warning(f"阻止访问内网地址: {hostname}")
                    return False
            
            return True
        except Exception as e:
            logger.error(f"URL验证失败: {e}")
            return False
    
    def _create_chrome_options(self) -> Options:
        """创建Chrome浏览器选项"""
        options = Options()
        
        # 安全配置
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--disable-software-rasterizer')
        options.add_argument('--disable-background-timer-throttling')
        options.add_argument('--disable-backgrounding-occluded-windows')
        options.add_argument('--disable-renderer-backgrounding')
        options.add_argument('--disable-features=TranslateUI')
        options.add_argument('--disable-ipc-flooding-protection')
        
        # 隐私和安全
        options.add_argument('--incognito')
        options.add_argument('--disable-extensions')
        options.add_argument('--disable-plugins')
        options.add_argument('--disable-images')
        options.add_argument('--disable-javascript')  # 默认禁用JS，需要时开启
        
        # 性能优化
        options.add_argument('--memory-pressure-off')
        options.add_argument('--max_old_space_size=4096')
        
        # 设置User-Agent
        options.add_argument(f'--user-agent={self.ua.random}')
        
        # 设置窗口大小
        options.add_argument('--window-size=1920,1080')
        
        # 在容器中运行
        if os.getenv('DISPLAY'):
            options.add_argument('--headless')
        
        return options
    
    @contextmanager
    def _get_driver(self, enable_javascript: bool = False):
        """获取浏览器驱动上下文管理器"""
        driver = None
        try:
            with self._driver_lock:
                options = self._create_chrome_options()
                
                # 根据需要启用JavaScript
                if enable_javascript:
                    options.add_argument('--enable-javascript')
                
                # 使用undetected-chromedriver避免检测
                driver = uc.Chrome(options=options, version_main=None)
                
                # 设置超时
                driver.set_page_load_timeout(30)
                driver.implicitly_wait(10)
                
                yield driver
                
        except Exception as e:
            logger.error(f"创建浏览器驱动失败: {e}")
            raise
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception as e:
                    logger.error(f"关闭浏览器失败: {e}")
    
    def scrape_page(self, url: str, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """抓取单个页面"""
        config = config or {}
        
        # 验证URL
        if not self._validate_url(url):
            return {
                'success': False,
                'error': 'URL验证失败',
                'url': url
            }
        
        # 检查缓存
        cache_key = f"scrape:{hash(url + str(config))}"
        cached = self.redis_client.get(cache_key)
        if cached and not config.get('force_refresh', False):
            logger.info(f"使用缓存结果: {url}")
            return json.loads(cached)
        
        start_time = time.time()
        result = {
            'success': False,
            'url': url,
            'timestamp': datetime.now().isoformat(),
            'content': '',
            'title': '',
            'meta': {},
            'links': [],
            'images': [],
            'execution_time': 0
        }
        
        try:
            with self._get_driver(config.get('enable_javascript', False)) as driver:
                logger.info(f"开始抓取: {url}")
                
                # 访问页面
                driver.get(url)
                
                # 等待页面加载
                wait_time = config.get('wait_time', 3)
                if wait_time > 0:
                    time.sleep(wait_time)
                
                # 等待特定元素（如果指定）
                wait_element = config.get('wait_element')
                if wait_element:
                    try:
                        WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, wait_element))
                        )
                    except TimeoutException:
                        logger.warning(f"等待元素超时: {wait_element}")
                
                # 获取页面内容
                page_source = driver.page_source
                
                # 检查页面大小
                if len(page_source) > self.max_page_size:
                    logger.warning(f"页面过大，截断处理: {len(page_source)} bytes")
                    page_source = page_source[:self.max_page_size]
                
                # 解析内容
                soup = BeautifulSoup(page_source, 'html.parser')
                
                # 提取标题
                title_elem = soup.find('title')
                result['title'] = title_elem.text.strip() if title_elem else ''
                
                # 提取主要内容
                content_selectors = config.get('content_selectors', [
                    'article', 'main', '.content', '#content',
                    '.post-content', '.entry-content'
                ])
                
                content_parts = []
                for selector in content_selectors:
                    elements = soup.select(selector)
                    for elem in elements:
                        text = elem.get_text(strip=True)
                        if text and len(text) > 50:
                            content_parts.append(text)
                
                # 如果没有找到主要内容，提取body文本
                if not content_parts:
                    body = soup.find('body')
                    if body:
                        content_parts.append(body.get_text(strip=True))
                
                result['content'] = '\n\n'.join(content_parts)
                
                # 提取链接
                if config.get('extract_links', False):
                    links = []
                    for link in soup.find_all('a', href=True):
                        href = urljoin(url, link['href'])
                        if self._validate_url(href):
                            links.append({
                                'url': href,
                                'text': link.get_text(strip=True)
                            })
                    result['links'] = links[:100]  # 限制数量
                
                # 提取图片
                if config.get('extract_images', False):
                    images = []
                    for img in soup.find_all('img', src=True):
                        src = urljoin(url, img['src'])
                        images.append({
                            'url': src,
                            'alt': img.get('alt', ''),
                            'title': img.get('title', '')
                        })
                    result['images'] = images[:50]  # 限制数量
                
                # 提取元数据
                meta_tags = soup.find_all('meta')
                for meta in meta_tags:
                    name = meta.get('name') or meta.get('property')
                    content = meta.get('content')
                    if name and content:
                        result['meta'][name] = content
                
                result['success'] = True
                execution_time = time.time() - start_time
                result['execution_time'] = execution_time
                
                logger.info(f"抓取成功: {url} ({execution_time:.2f}s)")
                
                # 缓存结果
                cache_ttl = config.get('cache_ttl', 3600)  # 1小时
                self.redis_client.setex(cache_key, cache_ttl, json.dumps(result))
                
        except TimeoutException:
            result['error'] = '页面加载超时'
            logger.error(f"抓取超时: {url}")
        except WebDriverException as e:
            result['error'] = f'浏览器错误: {str(e)}'
            logger.error(f"浏览器错误: {url}, {e}")
        except Exception as e:
            result['error'] = f'抓取失败: {str(e)}'
            logger.error(f"抓取失败: {url}, {e}")
        
        return result
    
    def scrape_multiple(self, urls: List[str], config: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        """批量抓取多个页面"""
        results = []
        
        for url in urls:
            try:
                result = self.scrape_page(url, config)
                results.append(result)
                
                # 延迟避免过快请求
                delay = config.get('delay', 1) if config else 1
                if delay > 0:
                    time.sleep(delay)
                    
            except Exception as e:
                logger.error(f"批量抓取失败: {url}, {e}")
                results.append({
                    'success': False,
                    'url': url,
                    'error': f'抓取失败: {str(e)}'
                })
        
        return results


# Flask服务
app = Flask(__name__)
scraper = BrowserScraper(os.getenv('REDIS_URL'))


@app.route('/health', methods=['GET'])
def health_check():
    """健康检查"""
    return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()})


@app.route('/scrape', methods=['POST'])
def scrape_endpoint():
    """抓取接口"""
    try:
        data = request.get_json()
        
        if not data or 'url' not in data:
            return jsonify({'error': '缺少URL参数'}), 400
        
        url = data['url']
        config = data.get('config', {})
        
        result = scraper.scrape_page(url, config)
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"抓取接口错误: {e}")
        return jsonify({'error': f'服务器错误: {str(e)}'}), 500


@app.route('/scrape/batch', methods=['POST'])
def scrape_batch_endpoint():
    """批量抓取接口"""
    try:
        data = request.get_json()
        
        if not data or 'urls' not in data:
            return jsonify({'error': '缺少URLs参数'}), 400
        
        urls = data['urls']
        config = data.get('config', {})
        
        # 限制批量数量
        if len(urls) > 10:
            return jsonify({'error': '批量抓取数量不能超过10个'}), 400
        
        results = scraper.scrape_multiple(urls, config)
        return jsonify({'results': results})
        
    except Exception as e:
        logger.error(f"批量抓取接口错误: {e}")
        return jsonify({'error': f'服务器错误: {str(e)}'}), 500


if __name__ == '__main__':
    logger.info("启动浏览器抓取服务...")
    app.run(host='0.0.0.0', port=8080, debug=False)