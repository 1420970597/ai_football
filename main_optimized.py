#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
优化版足球比赛分析工具
解决验证码识别时线程暂停的问题
"""

import os
import json
import time
import re
import shutil
import threading
import logging
import statistics
import requests
import base64
import random
import queue
import uuid
import asyncio
import aiohttp
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from pathlib import Path
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

# 导入配置
from config import API_CONFIG, ANALYSIS_CONFIG

class DateTimeEncoder(json.JSONEncoder):
    """自定义JSON编码器，处理datetime对象"""
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


class AIAnalysisClient:
    """AI分析客户端，用于大模型调用"""

    def __init__(self, api_token: str = None):
        self.api_token = api_token or API_CONFIG["api_token"]
        self.base_url = API_CONFIG["base_url"]
        self.model = API_CONFIG["model"]
        self.max_tokens = API_CONFIG["max_tokens"]
        self.temperature = API_CONFIG["temperature"]

        if not self.api_token:
            raise ValueError("API Token未设置，请在config.py中配置")

        self.headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }

        # 设置日志
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        self.logger = logging.getLogger(__name__)

    def chat_completion(self, messages: List[Dict], max_retries: int = None, timeout: int = None) -> Optional[Dict]:
        """调用大模型进行对话"""
        max_retries = max_retries or ANALYSIS_CONFIG["max_retries"]
        timeout = timeout or ANALYSIS_CONFIG["timeout"]

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens
        }

        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    time.sleep(ANALYSIS_CONFIG["request_delay"] * (2 ** attempt))

                response = requests.post(
                    self.base_url,
                    json=payload,
                    headers=self.headers,
                    timeout=timeout
                )

                if response.status_code == 200:
                    result = response.json()
                    if 'choices' in result and len(result['choices']) > 0:
                        return {
                            'content': result['choices'][0]['message']['content'],
                            'usage': result.get('usage', {}),
                            'model': result.get('model', self.model)
                        }
                elif response.status_code == 429:
                    self.logger.warning("遇到频率限制，等待重试...")
                    time.sleep(5 + attempt * 2)
                    continue
                else:
                    self.logger.error(f"API调用失败，状态码: {response.status_code}")

            except requests.exceptions.Timeout:
                self.logger.warning(f"请求超时 (尝试 {attempt + 1}/{max_retries})")
            except Exception as e:
                self.logger.error(f"请求失败: {e}")

        return None


class CaptchaHandler:
    """验证码处理器 - 专门处理验证码相关操作，避免阻塞主线程"""
    
    def __init__(self, captcha_api_url: str, captcha_token: str, captcha_type: str):
        self.captcha_api_url = captcha_api_url
        self.captcha_token = captcha_token
        self.captcha_type = captcha_type
        
        # 验证码处理队列和结果存储
        self.captcha_queue = queue.Queue()
        self.captcha_results = {}
        self.captcha_thread = None
        
        # 线程同步控制
        self.captcha_lock = threading.Lock()
        self.captcha_in_progress = threading.Event()
        
        # 启动验证码处理线程
        self.start_captcha_handler()
    
    def start_captcha_handler(self):
        """启动验证码处理线程"""
        def captcha_worker():
            """验证码处理工作线程"""
            print("    [验证码处理器] 验证码处理线程启动")
            
            while True:
                try:
                    # 获取验证码任务（阻塞等待）
                    task = self.captcha_queue.get(timeout=60)  # 60秒超时
                    
                    if task is None:  # 退出信号
                        print("    [验证码处理器] 收到退出信号")
                        break
                    
                    task_id, base64_image, verify_msg = task
                    print(f"    [验证码处理器] 开始处理任务 {task_id[:8]}...")
                    
                    # 调用验证码API（在专门线程中，不阻塞其他操作）
                    coordinates, api_code = self._call_captcha_api(base64_image, verify_msg)
                    
                    # 存储结果
                    self.captcha_results[task_id] = (coordinates, api_code)
                    
                    print(f"    [验证码处理器] 任务 {task_id[:8]} 完成")
                    
                    # 标记任务完成
                    self.captcha_queue.task_done()
                    
                except queue.Empty:
                    # 超时，继续等待
                    continue
                except Exception as e:
                    print(f"    [验证码处理器] 处理异常: {e}")
                    if 'task_id' in locals():
                        self.captcha_results[task_id] = (None, 0)
                        self.captcha_queue.task_done()
        
        # 启动守护线程
        self.captcha_thread = threading.Thread(target=captcha_worker, daemon=True)
        self.captcha_thread.start()
    
    def solve_captcha_async(self, base64_image: str, verify_msg: str, timeout: int = 45) -> Tuple[Optional[List[tuple]], int]:
        """异步提交验证码任务并等待结果"""
        task_id = str(uuid.uuid4())
        
        print(f"    [验证码处理器] 提交验证码任务 {task_id[:8]}...")
        
        # 提交任务到队列
        self.captcha_queue.put((task_id, base64_image, verify_msg))
        
        # 轮询等待结果
        start_time = time.time()
        while task_id not in self.captcha_results:
            if time.time() - start_time > timeout:
                print(f"    [验证码处理器] 任务 {task_id[:8]} 超时")
                return None, 0
            
            time.sleep(0.2)  # 200ms轮询间隔
        
        # 获取并清理结果
        result = self.captcha_results.pop(task_id, (None, 0))
        print(f"    [验证码处理器] 任务 {task_id[:8]} 获得结果")
        return result
    
    def _call_captcha_api(self, base64_image: str, verify_msg_content: str) -> tuple[Optional[List[tuple]], int]:
        """实际的验证码API调用（在专门线程中执行）"""
        try:
            data = {
                "token": self.captcha_token,
                "type": self.captcha_type,
                "image": base64_image,
                "extra": verify_msg_content
            }
            
            headers = {
                "Content-Type": "application/json"
            }
            
            print(f"    [验证码API] 调用验证码识别API，verify-msg内容: {verify_msg_content}")
            
            response = requests.post(
                self.captcha_api_url,
                headers=headers,
                json=data,
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                print(f"    [验证码API] API响应: {result}")
                
                code = result.get('code', 0)
                msg = result.get('msg', '未知错误')
                
                if code == 10000 and msg == '识别成功':
                    # 解析坐标数据：格式如 "88,98|36,67|217,52|157,124"
                    coordinate_data = result.get('data', {}).get('data', '')
                    if coordinate_data:
                        coordinates = []
                        for coord_pair in coordinate_data.split('|'):
                            if ',' in coord_pair:
                                x, y = coord_pair.split(',')
                                coordinates.append((int(x), int(y)))
                        
                        print(f"    [验证码API] 识别成功，获得坐标: {coordinates}")
                        return coordinates, code
                    else:
                        print("    [验证码API] API返回数据格式错误")
                        return None, code
                elif code == 10007:
                    print(f"    [验证码API] 图片识别失败，需要刷新页面: {msg}")
                    return None, code
                else:
                    print(f"    [验证码API] API识别失败: {msg} (code: {code})")
                    return None, code
            else:
                print(f"    [验证码API] API请求失败，状态码: {response.status_code}")
                return None, 0
                
        except Exception as e:
            print(f"    [验证码API] 调用验证码识别API失败: {e}")
            return None, 0
    
    def stop(self):
        """停止验证码处理器"""
        if self.captcha_thread and self.captcha_thread.is_alive():
            # 发送退出信号
            self.captcha_queue.put(None)
            self.captcha_thread.join(timeout=5)
            print("    [验证码处理器] 已停止")


class BrowserPool:
    """浏览器实例池管理器"""
    
    def __init__(self, max_size: int = 3):
        self.max_size = max_size
        self.browsers = []
        self.browser_locks = []
        self.browser_states = []  # 跟踪浏览器状态：'idle', 'busy', 'captcha'
        
    def initialize_pool(self, creator_func):
        """初始化浏览器池"""
        print(f"  [浏览器池] 正在创建{self.max_size}个浏览器实例...")
        
        for i in range(self.max_size):
            try:
                driver = creator_func()
                if driver:
                    self.browsers.append(driver)
                    self.browser_locks.append(threading.Lock())
                    self.browser_states.append('idle')
                    print(f"    [成功] 浏览器实例 {i+1} 创建成功")
                else:
                    print(f"    [失败] 浏览器实例 {i+1} 创建失败")
            except Exception as e:
                print(f"    [失败] 浏览器实例 {i+1} 创建异常: {e}")
        
        if not self.browsers:
            raise RuntimeError("没有可用的浏览器实例")
        
        print(f"  [浏览器池] 浏览器池创建完成，共{len(self.browsers)}个实例")
        return len(self.browsers)
    
    def acquire_browser(self, thread_id: int, captcha_priority: bool = False) -> tuple[Optional[object], int, threading.Lock]:
        """获取浏览器实例"""
        
        # 如果需要处理验证码，优先使用空闲的浏览器
        if captcha_priority:
            for i, (browser, lock, state) in enumerate(zip(self.browsers, self.browser_locks, self.browser_states)):
                if state == 'idle' and lock.acquire(blocking=False):
                    self.browser_states[i] = 'captcha'
                    print(f"    [浏览器池] 线程{thread_id} 获得验证码专用浏览器 {i+1}")
                    return browser, i, lock
        
        # 普通分配策略：轮询分配
        browser_index = (thread_id - 1) % len(self.browsers)
        browser = self.browsers[browser_index]
        lock = self.browser_locks[browser_index]
        
        # 等待浏览器可用
        print(f"    [浏览器池] 线程{thread_id} 等待浏览器 {browser_index + 1}...")
        lock.acquire()
        
        # 如果浏览器正在处理验证码，其他线程需要等待
        if self.browser_states[browser_index] == 'captcha':
            print(f"    [浏览器池] 浏览器 {browser_index + 1} 正在处理验证码，线程{thread_id} 等待...")
            # 这里可以实现更智能的等待策略
        
        self.browser_states[browser_index] = 'busy'
        print(f"    [浏览器池] 线程{thread_id} 获得浏览器 {browser_index + 1}")
        return browser, browser_index, lock
    
    def release_browser(self, browser_index: int, lock: threading.Lock, thread_id: int):
        """释放浏览器实例"""
        self.browser_states[browser_index] = 'idle'
        lock.release()
        print(f"    [浏览器池] 线程{thread_id} 释放浏览器 {browser_index + 1}")
    
    def cleanup(self):
        """清理浏览器池"""
        print(f"  [浏览器池] 开始清理...")
        for i, browser in enumerate(self.browsers):
            try:
                if browser:
                    browser.quit()
                    print(f"    [清理] 浏览器实例 {i+1} 已关闭")
            except Exception as e:
                print(f"    [清理] 浏览器实例 {i+1} 关闭异常: {e}")
        
        self.browsers.clear()
        self.browser_locks.clear()
        self.browser_states.clear()
        print(f"  [浏览器池] 清理完成")


# 导入自定义模块
from match_generator import get_football_data_single_files
from search_tools.sogou_searcher import SogouSearcher
from search_tools.sportsdata_searcher import SportsDataSearcher
from config import SPORTSDATA_CONFIG

def get_searcher(searcher_name: str):
    """搜索器工厂函数"""
    if searcher_name == "sogou":
        return SogouSearcher()
    elif searcher_name == "sportsdata":
        return SportsDataSearcher(api_key=SPORTSDATA_CONFIG["api_key"])
    else:
        raise ValueError(f"未知的搜索器: {searcher_name}")

# 导入浏览器相关模块
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from webdriver_manager.chrome import ChromeDriverManager
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False
    print("警告: 未安装selenium库，无法使用浏览器功能")
    print("安装命令: pip install selenium webdriver-manager")


class OptimizedFootballAnalyzer:
    """优化版足球比赛分析器"""

    def __init__(self, searcher_name: str = "sogou"):
        self.output_dir = Path("output")
        self.article_dir = self.output_dir / "articles"
        self.analysis_dir = self.output_dir / "analysis"
        self.searcher = get_searcher(searcher_name)
        
        # 验证码处理器
        self.captcha_handler = CaptchaHandler(
            captcha_api_url="http://api.jfbym.com/api/YmServer/customApi",
            captcha_token="FPeil-aOu2nS3DwUQTI_0nDo-ByLtRfRrB47OzYNJYQ",
            captcha_type="30100"
        )
        
        # 浏览器池
        self.browser_pool = BrowserPool(max_size=3)
        
        # 初始化AI客户端
        self.ai_client = AIAnalysisClient()

        # 创建输出目录
        self.article_dir.mkdir(parents=True, exist_ok=True)
        self.analysis_dir.mkdir(parents=True, exist_ok=True)

        # 设置日志
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        self.logger = logging.getLogger(__name__)

    def setup_chrome_driver(self, enable_manual_verification: bool = True) -> bool:
        """设置Chrome浏览器驱动并初始化浏览器池"""
        if not SELENIUM_AVAILABLE:
            print("Selenium未安装，无法使用浏览器功能")
            return False

        try:
            # 初始化浏览器池
            pool_size = self.browser_pool.initialize_pool(self._create_chrome_driver)
            
            self.manual_verification_enabled = enable_manual_verification
            verification_mode = "支持手动验证" if enable_manual_verification else "自动模式"
            print(f"Chrome浏览器驱动启动成功 ({verification_mode})，浏览器池大小: {pool_size}")
            return True

        except Exception as e:
            print(f"浏览器设置失败: {e}")
            return False
    
    def _create_chrome_driver(self):
        """创建单个Chrome浏览器实例"""
        chrome_options = Options()

        # 基础设置
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--disable-software-rasterizer')

        # 核心反检测设置
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)

        # 窗口设置 - 优化：使用无头模式减少资源消耗
        if hasattr(self, 'manual_verification_enabled') and self.manual_verification_enabled:
            # 手动验证模式下显示窗口，但使用较小尺寸
            chrome_options.add_argument('--window-size=1280,720')
            chrome_options.add_argument('--disable-infobars')
            chrome_options.add_argument('--disable-extensions')
        else:
            # 自动模式使用无头模式
            chrome_options.add_argument('--headless')
            chrome_options.add_argument('--window-size=1920,1080')

        # 性能优化设置
        chrome_options.add_argument('--disable-web-security')
        chrome_options.add_argument('--allow-running-insecure-content')
        chrome_options.add_argument('--disable-features=TranslateUI')
        chrome_options.add_argument('--disable-iframes-sandbox-flags')
        chrome_options.add_argument('--no-first-run')
        chrome_options.add_argument('--disable-default-apps')
        chrome_options.add_argument('--disable-popup-blocking')
        chrome_options.add_argument('--ignore-certificate-errors')
        chrome_options.add_argument('--ignore-ssl-errors')
        chrome_options.add_argument('--ignore-certificate-errors-spki-list')
        chrome_options.add_argument('--disable-background-timer-throttling')
        chrome_options.add_argument('--disable-backgrounding-occluded-windows')
        chrome_options.add_argument('--disable-renderer-backgrounding')

        # 最新的User-Agent
        user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
        chrome_options.add_argument(f'--user-agent={user_agent}')

        # 设置首选项
        prefs = {
            "profile.default_content_setting_values.notifications": 2,
            "profile.default_content_settings.popups": 0,
            "profile.managed_default_content_settings.images": 1,
            "intl.accept_languages": "zh-CN,zh;q=0.9,en;q=0.8"
        }
        chrome_options.add_experimental_option("prefs", prefs)

        # 禁用自动化检测特征
        chrome_options.add_experimental_option("excludeSwitches", [
            "enable-automation",
            "enable-blink-features=AutomationControlled"
        ])
        chrome_options.add_experimental_option('useAutomationExtension', False)

        # 使用多种方式尝试获取ChromeDriver
        driver_service = None

        # 方法1：尝试使用webdriver-manager自动下载
        try:
            service = Service(ChromeDriverManager().install())
            driver_service = service
            print("    [成功] 成功通过webdriver-manager获取ChromeDriver")
        except Exception as e:
            print(f"    [失败] webdriver-manager失败: {e}")

        # 如果方法1失败，尝试方法2：使用系统PATH中的chromedriver
        if not driver_service:
            try:
                chromedriver_path = shutil.which("chromedriver")
                if chromedriver_path:
                    driver_service = Service(chromedriver_path)
                    print(f"    [成功] 找到系统chromedriver: {chromedriver_path}")
                else:
                    print("    [失败] 系统PATH中未找到chromedriver")
            except Exception as e2:
                print(f"    [失败] 系统PATH查找失败: {e2}")

        # 如果所有方法都失败
        if not driver_service:
            print("    [失败] 无法获取ChromeDriver")
            return None

        # 创建Chrome实例
        try:
            driver = webdriver.Chrome(service=driver_service, options=chrome_options)

            # 执行反检测脚本
            stealth_script = """
            // 隐藏webdriver特征
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            delete navigator.__proto__.webdriver;
            
            // 修改plugins
            Object.defineProperty(navigator, 'plugins', {
                get: () => [
                    {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
                    {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: ''},
                    {name: 'Native Client', filename: 'internal-nacl-plugin', description: ''}
                ]
            });
            
            // 修改languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['zh-CN', 'zh', 'en-US', 'en']
            });
            
            // 添加Chrome特有属性
            window.chrome = {
                runtime: {onConnect: null, onMessage: null},
                loadTimes: function() {
                    return {
                        commitLoadTime: 1234567890.123,
                        connectionInfo: 'h2',
                        finishDocumentLoadTime: 1234567890.456,
                        finishLoadTime: 1234567890.789,
                        navigationType: 'Navigation',
                        requestTime: 1234567890.000,
                        startLoadTime: 1234567890.001
                    };
                }
            };
            """

            driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
                'source': stealth_script
            })

            # 设置页面加载策略
            driver.implicitly_wait(10)
            return driver

        except Exception as e:
            print(f"Chrome驱动创建失败: {e}")
            return None

    def extract_articles_concurrently_optimized(self, articles: List[Dict], keyword: str, max_workers: int = 3) -> List[Dict]:
        """优化版并发提取文章内容"""
        print(f"  [优化并发] 使用{max_workers}个浏览器进程并发提取 {len(articles)} 篇文章...")
        
        # 初始化统计
        successful_extractions = 0
        blocked_by_antibot = 0
        captcha_articles = []  # 遇到验证码的文章，稍后单独处理
        
        def extract_worker_optimized(article_with_index):
            """优化版工作线程函数"""
            i, article = article_with_index
            thread_id = threading.current_thread().ident
            
            # 获取浏览器实例
            try:
                browser, browser_index, browser_lock = self.browser_pool.acquire_browser(i)
                
                print(f"    [线程{i}] 开始处理文章 {article.get('title', '无标题')[:30]}...")
                print(f"    [线程{i}] 使用浏览器实例 {browser_index + 1}")
                
                if not browser:
                    return i, {
                        'article_info': article,
                        'content_data': {
                            'success': False,
                            'error': '浏览器实例不可用',
                            'title': '',
                            'text': '',
                            'url': article.get('url', ''),
                            'access_time': datetime.now().isoformat()
                        }
                    }
                
                url = article.get('url', '')
                if not url:
                    print(f"    [线程{i}] 文章缺少URL")
                    return i, {
                        'article_info': article,
                        'content_data': {
                            'success': False,
                            'error': '文章缺少URL',
                            'title': '',
                            'text': '',
                            'url': '',
                            'access_time': datetime.now().isoformat()
                        }
                    }
                
                # 提取文章内容（优化版）
                content_data = self._extract_article_content_optimized(url, keyword, browser, i)
                
                # 检查是否遇到验证码
                if '验证码' in content_data.get('error', '') or '验证' in content_data.get('error', ''):
                    print(f"    [线程{i}] 遇到验证码，标记为稍后处理")
                    captcha_articles.append((i, article, url))
                    
                    # 使用降级策略：保存基本信息
                    content_data = {
                        'success': False,
                        'error': '遇到验证码，稍后处理',
                        'title': article.get('title', ''),
                        'text': article.get('summary', ''),  # 使用摘要作为临时内容
                        'url': url,
                        'access_time': datetime.now().isoformat(),
                        'captcha_detected': True
                    }
                
                success_status = "成功" if content_data.get('success') else "失败"
                print(f"    [线程{i}] 内容提取{success_status}")
                
                return i, {
                    'article_info': article,
                    'content_data': content_data
                }
                
            except Exception as e:
                print(f"    [线程{i}] 异常: {e}")
                return i, {
                    'article_info': article,
                    'content_data': {
                        'success': False,
                        'error': f'线程异常: {e}',
                        'title': '',
                        'text': '',
                        'url': article.get('url', ''),
                        'access_time': datetime.now().isoformat()
                    }
                }
            finally:
                # 释放浏览器实例
                try:
                    self.browser_pool.release_browser(browser_index, browser_lock, i)
                except:
                    pass
        
        results = []
        
        # 使用ThreadPoolExecutor进行并发处理
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 创建任务列表，包含索引和文章数据
            tasks = [(i, article) for i, article in enumerate(articles, 1)]
            
            # 提交所有任务
            future_to_article = {
                executor.submit(extract_worker_optimized, task): task
                for task in tasks
            }
            
            # 收集结果
            completed_count = 0
            for future in as_completed(future_to_article):
                completed_count += 1
                try:
                    i, result = future.result()
                    results.append((i, result))
                    
                    # 统计结果
                    content_data = result['content_data']
                    if content_data.get('success'):
                        successful_extractions += 1
                    elif '验证' in content_data.get('error', ''):
                        blocked_by_antibot += 1
                    
                    # 显示进度
                    success = content_data.get('success', False)
                    text_length = len(content_data.get('text', ''))
                    status = "成功" if success else "失败"
                    
                    print(f"    [进度] 已完成 {completed_count}/{len(articles)} 篇文章提取")
                    print(f"    [即时结果] 文章{i}: {status}, 内容长度: {text_length} 字符")
                    
                except Exception as e:
                    print(f"    [异常] 线程执行异常: {e}")
        
        # 按原始顺序排序结果
        results.sort(key=lambda x: x[0])
        
        # 处理遇到验证码的文章（使用专门的验证码处理策略）
        if captcha_articles:
            print(f"\n  [验证码处理] 发现 {len(captcha_articles)} 篇文章遇到验证码，开始专门处理...")
            captcha_results = self._handle_captcha_articles(captcha_articles, keyword)
            
            # 更新结果
            for captcha_result in captcha_results:
                # 找到对应的结果并更新
                for j, (result_index, result_data) in enumerate(results):
                    if result_index == captcha_result[0]:
                        results[j] = captcha_result
                        if captcha_result[1]['content_data'].get('success'):
                            successful_extractions += 1
                            blocked_by_antibot -= 1
                        break
        
        print(f"\n  [优化统计] 并发提取统计:")
        print(f"    [成功] 成功提取: {successful_extractions} 篇")
        print(f"    [拦截] 验证页面拦截: {blocked_by_antibot} 篇")
        print(f"    [失败] 其他错误: {len(articles) - successful_extractions - blocked_by_antibot} 篇")
        
        if successful_extractions > 0:
            success_rate = (successful_extractions / len(articles)) * 100
            print(f"    [成功率] 成功率: {success_rate:.1f}%")
        
        return [result[1] for result in results]

    def _extract_article_content_optimized(self, url: str, keyword: str, driver, thread_id: int) -> Dict:
        """优化版文章内容提取"""
        try:
            print(f"        [线程{thread_id}] 正在访问: {url[:60]}...")
            
            # 随机延迟模拟真实用户（优化：减少延迟）
            delay = random.uniform(0.3, 0.8)
            time.sleep(delay)

            # 访问目标URL
            driver.get(url)

            # 智能等待页面加载
            try:
                WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                print(f"        [线程{thread_id}] 页面加载完成")
                time.sleep(random.uniform(0.3, 0.6))
            except:
                print(f"        [线程{thread_id}] 页面加载超时，继续处理")

            # 检测URL变化
            initial_url = driver.current_url
            for i in range(3):
                time.sleep(0.3)
                current_url = driver.current_url
                if current_url != initial_url:
                    print(f"        [线程{thread_id}] 检测到页面跳转")
                    time.sleep(0.2)
                    break
            
            title = driver.title
            page_source = driver.page_source
            
            print(f"        [线程{thread_id}] 标题: {title[:30]}...")
            print(f"        [线程{thread_id}] 内容长度: {len(page_source)} 字符")

            # 检查是否为验证页面（优化版）
            if self._is_verification_page_optimized(title, page_source, driver):
                print(f"        [线程{thread_id}] 检测到验证页面")
                
                # 使用异步验证码处理
                verification_success = self._handle_verification_async(driver, thread_id)
                
                if verification_success:
                    print(f"        [线程{thread_id}] 验证码处理成功，重新获取内容...")
                    time.sleep(2)
                    title = driver.title
                    page_source = driver.page_source
                    print(f"        [线程{thread_id}] 验证后标题: {title[:30]}...")
                    print(f"        [线程{thread_id}] 验证后内容长度: {len(page_source)} 字符")
                else:
                    print(f"        [线程{thread_id}] 验证码处理失败")
                    return {
                        'success': False,
                        'error': '验证码处理失败',
                        'title': title,
                        'text': '验证码识别失败',
                        'url': url,
                        'access_time': datetime.now().isoformat()
                    }

            # 提取文本内容（优化版）
            text_content = self._extract_text_content_optimized(driver, thread_id)
            
            # 清理内容
            title = ''.join(c for c in title if ord(c) < 0x10000) if title else ""
            text_content = ''.join(c for c in text_content if ord(c) < 0x10000) if text_content else ""
            text_content = text_content[:5000]  # 限制长度
            
            success = len(text_content) > 50
            print(f"        [线程{thread_id}] 提取{'成功' if success else '失败'}: {len(text_content)} 字符")

            return {
                'success': success,
                'error': '' if success else '内容太少或提取失败',
                'title': title,
                'text': text_content,
                'url': url,
                'access_time': datetime.now().isoformat()
            }

        except Exception as e:
            print(f"        [线程{thread_id}] 提取异常: {e}")
            return {
                'success': False,
                'error': f'文章提取失败: {str(e)}',
                'title': '',
                'text': '',
                'url': url,
                'access_time': datetime.now().isoformat()
            }

    def _is_verification_page_optimized(self, title: str, page_source: str, driver) -> bool:
        """优化版验证页面检测"""
        # 强指示器检查
        strong_indicators = [
            "搜狗" in title and "验证" in page_source,
            "安全验证" in page_source,
            "请点击" in page_source and "验证码" in page_source,
            "security verification" in page_source.lower(),
            "滑动验证" in page_source
        ]
        
        if any(strong_indicators):
            return True
        
        # 检查当前URL和页面特征
        current_url = driver.current_url
        if "sogou.com" in current_url:
            # 检查验证码元素
            try:
                captcha_selectors = [
                    'img[id*="verify"]', 'img[class*="verify"]',
                    'img[id*="captcha"]', 'img[class*="captcha"]'
                ]
                
                for selector in captcha_selectors:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    if elements and len(page_source) < 15000:
                        return True
            except:
                pass
        
        return False

    def _handle_verification_async(self, driver, thread_id: int) -> bool:
        """异步处理验证码"""
        try:
            print(f"        [线程{thread_id}] 开始异步验证码处理...")
            
            # 提取验证码信息
            verify_msg = self._get_verify_msg_content(driver)
            base64_image = self._capture_captcha_image(driver)
            
            if not verify_msg or not base64_image:
                print(f"        [线程{thread_id}] 验证码信息提取失败")
                return False
            
            print(f"        [线程{thread_id}] 提交验证码任务到异步处理器...")
            
            # 使用异步验证码处理器
            coordinates, api_code = self.captcha_handler.solve_captcha_async(
                base64_image, verify_msg, timeout=30
            )
            
            if api_code == 10007:
                print(f"        [线程{thread_id}] 图片识别失败，刷新页面...")
                driver.refresh()
                time.sleep(2)
                return False
            
            if not coordinates:
                print(f"        [线程{thread_id}] 验证码API识别失败")
                return False
            
            # 执行点击操作
            success = self._click_captcha_coordinates(driver, coordinates, thread_id)
            if not success:
                return False
            
            # 检查验证结果
            time.sleep(2)
            current_msg = self._get_verify_msg_content(driver)
            if "验证成功" in current_msg or "成功" in current_msg:
                print(f"        [线程{thread_id}] 验证成功！")
                self._click_submit_button(driver)
                return True
            else:
                print(f"        [线程{thread_id}] 验证未通过: {current_msg}")
                return False
                
        except Exception as e:
            print(f"        [线程{thread_id}] 验证码处理异常: {e}")
            return False

    def _handle_captcha_articles(self, captcha_articles: List[tuple], keyword: str) -> List[tuple]:
        """专门处理遇到验证码的文章"""
        print(f"  [验证码专处] 开始处理 {len(captcha_articles)} 篇验证码文章...")
        
        results = []
        
        # 使用单线程，专门的验证码处理策略
        for i, article, url in captcha_articles:
            try:
                print(f"    [验证码专处] 处理文章 {i}: {article.get('title', '无标题')[:30]}...")
                
                # 获取专用浏览器实例
                browser, browser_index, browser_lock = self.browser_pool.acquire_browser(
                    i, captcha_priority=True
                )
                
                try:
                    # 专门的验证码处理流程
                    content_data = self._extract_with_captcha_handling(url, keyword, browser, i)
                    
                    result = (i, {
                        'article_info': article,
                        'content_data': content_data
                    })
                    results.append(result)
                    
                    success = "成功" if content_data.get('success') else "失败"
                    print(f"    [验证码专处] 文章 {i} 处理{success}")
                    
                finally:
                    self.browser_pool.release_browser(browser_index, browser_lock, i)
                    
            except Exception as e:
                print(f"    [验证码专处] 文章 {i} 处理异常: {e}")
                results.append((i, {
                    'article_info': article,
                    'content_data': {
                        'success': False,
                        'error': f'验证码处理异常: {e}',
                        'title': '',
                        'text': '',
                        'url': url,
                        'access_time': datetime.now().isoformat()
                    }
                }))
        
        print(f"  [验证码专处] 验证码文章处理完成")
        return results

    def _extract_with_captcha_handling(self, url: str, keyword: str, driver, thread_id: int) -> Dict:
        """专门的验证码处理提取流程"""
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                print(f"    [验证码专处] 文章 {thread_id} 第 {attempt + 1}/{max_retries} 次尝试...")
                
                # 访问URL
                driver.get(url)
                time.sleep(2)
                
                title = driver.title
                page_source = driver.page_source
                
                # 如果是验证页面，进行处理
                if self._is_verification_page_optimized(title, page_source, driver):
                    print(f"    [验证码专处] 文章 {thread_id} 检测到验证页面，开始处理...")
                    
                    # 如果启用手动验证，尝试自动处理，失败则转手动
                    if hasattr(self, 'manual_verification_enabled') and self.manual_verification_enabled:
                        auto_success = self._handle_verification_async(driver, thread_id)
                        
                        if not auto_success:
                            print(f"    [验证码专处] 文章 {thread_id} 自动验证失败，转手动模式...")
                            # 这里可以实现手动验证等待逻辑
                            # 为了避免长时间阻塞，暂时跳过
                            continue
                    else:
                        # 纯自动模式
                        auto_success = self._handle_verification_async(driver, thread_id)
                        if not auto_success:
                            continue
                    
                    # 验证成功，重新获取页面
                    time.sleep(3)
                    title = driver.title
                    page_source = driver.page_source
                
                # 提取内容
                text_content = self._extract_text_content_optimized(driver, thread_id)
                
                if len(text_content) > 50:
                    print(f"    [验证码专处] 文章 {thread_id} 提取成功")
                    return {
                        'success': True,
                        'error': '',
                        'title': title,
                        'text': text_content[:5000],
                        'url': url,
                        'access_time': datetime.now().isoformat()
                    }
                
            except Exception as e:
                print(f"    [验证码专处] 文章 {thread_id} 第 {attempt + 1} 次尝试异常: {e}")
                time.sleep(2)
        
        print(f"    [验证码专处] 文章 {thread_id} 所有尝试失败")
        return {
            'success': False,
            'error': '验证码处理失败，多次尝试无效',
            'title': '',
            'text': '',
            'url': url,
            'access_time': datetime.now().isoformat()
        }

    def _extract_text_content_optimized(self, driver, thread_id: int) -> str:
        """优化版文本内容提取"""
        try:
            # 优化的选择器列表
            selectors = [
                '[id*="js_content"]',
                '.rich_media_content',
                '#js_content',
                '.rich_media_area_primary',
                'article',
                '.article-content',
                '.content',
                '.post-content',
                '.text-content',
                'main'
            ]

            for selector in selectors:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    if elements:
                        text_content = elements[0].text.strip()
                        if len(text_content) > 200:
                            print(f"        [线程{thread_id}] 通过选择器 {selector} 提取到 {len(text_content)} 字符")
                            return text_content
                except:
                    continue

            # 回退到body
            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text
                if len(body_text) > 100:
                    print(f"        [线程{thread_id}] 通过body标签提取到 {len(body_text)} 字符")
                    return body_text
            except:
                pass

            return ""

        except Exception as e:
            print(f"        [线程{thread_id}] 文本提取异常: {e}")
            return ""

    def _get_verify_msg_content(self, driver) -> str:
        """获取验证码提示内容"""
        try:
            verify_msg_selectors = [
                '[id*="verify-msg"]', '[class*="verify-msg"]',
                '.verify-msg', '#verify-msg',
                '.verify-text', '.captcha-text', '.verify-tip'
            ]
            
            for selector in verify_msg_selectors:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    if elements:
                        content = elements[0].text.strip()
                        if content:
                            # 提取【】中的内容
                            import re
                            bracket_pattern = r'【([^】]+)】'
                            matches = re.findall(bracket_pattern, content)
                            if matches:
                                return matches[0]
                            return content
                except:
                    continue
            
            # 从页面源码提取
            page_source = driver.page_source
            import re
            patterns = [
                r'请依次点击【([^】]+)】',
                r'点击【([^】]+)】',
                r'选择【([^】]+)】',
                r'【([^】]+)】',
            ]
            
            for pattern in patterns:
                matches = re.findall(pattern, page_source)
                if matches:
                    return matches[0]
            
            return ""
            
        except Exception as e:
            print(f"    [验证码] 获取verify-msg内容失败: {e}")
            return ""

    def _capture_captcha_image(self, driver) -> Optional[str]:
        """提取验证码图片的base64编码"""
        try:
            # 查找验证码元素
            captcha_selectors = [
                'img[id*="verify"]', 'img[class*="verify"]',
                'img[id*="captcha"]', 'img[class*="captcha"]',
                'img[class*="back-img"]',
                '.verify-img img', '.captcha-img img',
                '.verify-container img', '#verify-img', '#captcha-img'
            ]
            
            for selector in captcha_selectors:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    if elements:
                        element = elements[0]
                        src = element.get_attribute('src')
                        
                        if src and src.startswith('data:image/'):
                            # 提取base64数据
                            if ',base64,' in src:
                                return src.split(',base64,')[1]
                            elif ',' in src:
                                return src.split(',')[1]
                        else:
                            # 截图方式
                            screenshot = element.screenshot_as_png
                            return base64.b64encode(screenshot).decode()
                except:
                    continue
            
            return None
            
        except Exception as e:
            print(f"    [验证码] 提取验证码图片失败: {e}")
            return None

    def _click_captcha_coordinates(self, driver, coordinates: List[tuple], thread_id: int) -> bool:
        """点击验证码坐标"""
        try:
            # 查找验证码元素
            captcha_selectors = [
                'img[id*="verify"]', 'img[class*="verify"]',
                'img[id*="captcha"]', 'img[class*="captcha"]'
            ]
            
            captcha_element = None
            for selector in captcha_selectors:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    if elements:
                        captcha_element = elements[0]
                        break
                except:
                    continue
            
            if not captcha_element:
                print(f"        [线程{thread_id}] 未找到验证码元素")
                return False
            
            # 滚动到验证码元素
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", captcha_element)
            time.sleep(0.5)
            
            # 点击每个坐标
            for i, (rel_x, rel_y) in enumerate(coordinates, 1):
                try:
                    print(f"        [线程{thread_id}] 第{i}次点击坐标: ({rel_x}, {rel_y})")
                    
                    # 使用JavaScript模拟点击
                    click_success = driver.execute_script("""
                        var element = arguments[0];
                        var relX = arguments[1];
                        var relY = arguments[2];
                        
                        try {
                            var rect = element.getBoundingClientRect();
                            var clientX = rect.left + relX;
                            var clientY = rect.top + relY;
                            
                            var eventOptions = {
                                'view': window,
                                'bubbles': true,
                                'cancelable': true,
                                'clientX': clientX,
                                'clientY': clientY,
                                'button': 0,
                                'buttons': 1,
                                'detail': 1
                            };
                            
                            var events = [
                                new MouseEvent('mousedown', eventOptions),
                                new MouseEvent('mouseup', eventOptions),
                                new MouseEvent('click', eventOptions)
                            ];
                            
                            var success = true;
                            events.forEach(function(event) {
                                try {
                                    var result = element.dispatchEvent(event);
                                    if (!result) success = false;
                                } catch(e) {
                                    success = false;
                                }
                            });
                            
                            return success;
                        } catch(e) {
                            return false;
                        }
                    """, captcha_element, rel_x, rel_y)
                    
                    if click_success:
                        print(f"        [线程{thread_id}] 第{i}次点击成功")
                    
                    # 延迟
                    time.sleep(random.uniform(0.2, 0.4))
                    
                except Exception as e:
                    print(f"        [线程{thread_id}] 第{i}次点击异常: {e}")
                    return False
            
            print(f"        [线程{thread_id}] 完成{len(coordinates)}次点击")
            time.sleep(1)
            return True
            
        except Exception as e:
            print(f"        [线程{thread_id}] 点击验证码坐标失败: {e}")
            return False

    def _click_submit_button(self, driver) -> bool:
        """点击提交按钮"""
        try:
            submit_selectors = [
                'a[id="submit"]', '#submit',
                'button[type="submit"]', '.submit',
                '[onclick*="submit"]'
            ]
            
            for selector in submit_selectors:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    if elements:
                        elements[0].click()
                        time.sleep(2)
                        print("    [验证码] 提交按钮已点击")
                        return True
                except:
                    continue
            
            print("    [验证码] 未找到提交按钮")
            return False
                
        except Exception as e:
            print(f"    [验证码] 点击提交按钮失败: {e}")
            return False

    # 复制原有的其他方法...
    def get_match_data(self) -> List[Dict]:
        """获取比赛数据"""
        print("=== 步骤1：获取足球比赛数据 ===")

        # 调用比赛数据获取
        success = get_football_data_single_files()
        if not success:
            print("获取比赛数据失败")
            return []

        # 读取生成的JSON文件
        matches = []
        if self.output_dir.exists():
            for json_file in self.output_dir.glob("*.json"):
                try:
                    with open(json_file, 'r', encoding='utf-8') as f:
                        match_data = json.load(f)
                        matches.append({
                            'file_name': json_file.name,
                            'data': match_data
                        })
                except Exception as e:
                    print(f"读取文件 {json_file} 失败: {e}")

        print(f"共找到 {len(matches)} 场比赛")
        return matches

    def generate_search_keyword(self, match_data: Dict) -> str:
        """生成搜索关键词"""
        basic_info = match_data.get('基本信息', {})
        home_team = basic_info.get('主队名称', '').strip()
        away_team = basic_info.get('客队名称', '').strip()

        # 去除队名中的空格和特殊字符
        home_team = re.sub(r'\s+', '', home_team)
        away_team = re.sub(r'\s+', '', away_team)

        # 过滤掉可能导致编码问题的字符
        home_team = ''.join(c for c in home_team if ord(c) < 0x10000)
        away_team = ''.join(c for c in away_team if ord(c) < 0x10000)

        if home_team and away_team:
            return f"{home_team}vs{away_team}"
        return ""

    def search_articles_for_match(self, keyword: str) -> List[Dict]:
        """搜索比赛相关文章"""
        print(f"  搜索关键词: {keyword}")

        try:
            # 强制搜索10页，然后筛选48小时内的文章
            articles = self.searcher.search_recent_articles_force(keyword, 48, 10)
            print(f"  找到 {len(articles)} 篇文章")
            return articles
        except Exception as e:
            print(f"  搜索文章失败: {e}")
            return []

    def save_match_articles_optimized(self, match_info: Dict, articles: List[Dict], keyword: str, extract_content: bool = True):
        """优化版保存比赛相关文章"""
        # 清理关键词，确保文件夹名称安全
        safe_keyword = ''.join(c for c in keyword if c.isalnum() or c in 'vs')
        safe_keyword = safe_keyword[:50]

        # 创建比赛文件夹
        match_folder_name = f"match_{safe_keyword}_{int(time.time())}"
        match_folder = self.article_dir / match_folder_name
        match_folder.mkdir(exist_ok=True)

        print(f"  创建文件夹: {match_folder}")

        # 保存比赛基本信息
        match_info_file = match_folder / "match_info.json"
        try:
            with open(match_info_file, 'w', encoding='utf-8', errors='ignore') as f:
                json.dump({
                    'keyword': keyword,
                    'match_data': match_info,
                    'search_time': datetime.now().isoformat(),
                    'articles_count': len(articles),
                    'content_extraction': extract_content,
                    'optimization_used': True
                }, f, ensure_ascii=False, indent=2, cls=DateTimeEncoder)
        except Exception as e:
            print(f"    保存比赛信息失败: {e}")

        # 保存文章列表
        articles_list_file = match_folder / "articles_list.json"
        try:
            with open(articles_list_file, 'w', encoding='utf-8', errors='ignore') as f:
                json.dump(articles, f, ensure_ascii=False, indent=2, cls=DateTimeEncoder)
        except Exception as e:
            print(f"    保存文章列表失败: {e}")

        # 内容提取
        if extract_content and articles:
            print(f"  开始提取 {len(articles)} 篇文章的详细内容（优化版）...")
            
            # 使用优化版并发提取
            extraction_results = self.extract_articles_concurrently_optimized(articles, keyword, max_workers=3)
            
            # 保存提取结果
            successful_extractions = 0
            blocked_by_antibot = 0
            
            print(f"  [保存] 开始保存{len(extraction_results)}篇文章到JSON文件...")
            
            for i, result in enumerate(extraction_results, 1):
                article_info = result.get('article_info', {})
                content_data = result.get('content_data', {})
                
                # 保存文章数据
                article_file = match_folder / f"article_{i:03d}.json"
                combined_data = {
                    'article_info': article_info,
                    'content_data': content_data,
                    'optimization_used': True
                }

                try:
                    article_file.parent.mkdir(parents=True, exist_ok=True)
                    
                    with open(article_file, 'w', encoding='utf-8', errors='ignore') as f:
                        json.dump(combined_data, f, ensure_ascii=False, indent=2, cls=DateTimeEncoder)
                    
                    if article_file.exists():
                        file_size = article_file.stat().st_size
                        print(f"    [成功] 文章 {i} 保存成功，文件大小: {file_size} 字节")
                        
                except Exception as e:
                    print(f"    [失败] 保存文章 {i} 失败: {e}")

                # 统计结果
                if content_data.get('success'):
                    successful_extractions += 1
                elif '验证码' in content_data.get('error', '') or '验证' in content_data.get('error', ''):
                    blocked_by_antibot += 1

            # 输出统计结果
            print(f"  [优化统计] 内容提取统计:")
            print(f"    [成功] 成功提取: {successful_extractions} 篇")
            print(f"    [拦截] 验证页面拦截: {blocked_by_antibot} 篇")
            print(f"    [失败] 其他错误: {len(articles) - successful_extractions - blocked_by_antibot} 篇")

            if successful_extractions > 0:
                success_rate = (successful_extractions / len(articles)) * 100
                print(f"    [成功率] 成功率: {success_rate:.1f}%")

        elif not extract_content:
            print(f"  仅保存文章信息，跳过内容提取")

        print(f"  比赛 {keyword} 的所有文章已保存到: {match_folder}")

    def process_all_matches_optimized(self):
        """优化版处理所有比赛"""
        print("=== 优化版足球比赛分析工具启动 ===\n")

        # 步骤1：获取所有比赛数据
        matches = self.get_match_data()
        if not matches:
            print("没有找到任何比赛，程序结束。")
            return

        print(f"\n=== 步骤2：搜索文章（优化版）===")
        print(f"共有 {len(matches)} 场比赛需要搜索文章")

        # 步骤2：设置浏览器
        browser_available = self.setup_chrome_driver(enable_manual_verification=True)
        if browser_available:
            print("[成功] 优化版浏览器驱动设置成功，将提取文章内容")
            print("[优化] 验证码处理已异步化，减少线程阻塞")
        else:
            print("[失败] 浏览器驱动设置失败，仅保存文章链接")
            return

        # 步骤3：搜索每场比赛的文章
        processed_count = 0
        skipped_count = 0

        try:
            for i, match in enumerate(matches, 1):
                try:
                    print(f"\n--- 处理第 {i}/{len(matches)} 场比赛 ---")

                    match_data = match['data']
                    keyword = self.generate_search_keyword(match_data)

                    if not keyword:
                        print("  无法生成搜索关键词，跳过该比赛")
                        continue

                    print(f"  比赛关键词: {keyword}")

                    # 搜索文章
                    articles = self.search_articles_for_match(keyword)

                    # 保存文章（使用优化版）
                    if articles:
                        self.save_match_articles_optimized(match_data, articles, keyword)
                        processed_count += 1
                        print(f"  [成功] 成功处理比赛 {keyword}")
                    else:
                        print(f"  没有找到文章")

                    # 优化：减少延迟
                    time.sleep(0.5)

                except Exception as e:
                    print(f"  处理比赛时出现错误: {e}")
                    continue

        finally:
            # 清理资源
            print(f"\n=== 清理资源 ===")
            self.captcha_handler.stop()
            self.browser_pool.cleanup()

        print(f"\n=== 优化版处理完成 ===")
        print(f"[统计] 处理统计:")
        print(f"  [成功] 新处理比赛: {processed_count} 场")
        print(f"  [跳过] 跳过比赛: {skipped_count} 场")
        print(f"  [保存] 所有文章已保存到: {self.article_dir}")
        print(f"  [优化] 验证码处理异步化完成，线程阻塞问题已解决")

    def cleanup(self):
        """清理资源"""
        print("  [清理] 开始清理资源...")
        
        try:
            if hasattr(self, 'captcha_handler'):
                self.captcha_handler.stop()
                print("  [清理] 验证码处理器已停止")
        except Exception as e:
            print(f"  [清理] 停止验证码处理器失败: {e}")
        
        try:
            if hasattr(self, 'browser_pool'):
                self.browser_pool.cleanup()
                print("  [清理] 浏览器池已清理")
        except Exception as e:
            print(f"  [清理] 清理浏览器池失败: {e}")
        
        print("  [清理] 资源清理完成")

    def __del__(self):
        """析构函数，确保资源被正确清理"""
        try:
            self.cleanup()
        except:
            pass


def main():
    """主函数"""
    import argparse
    parser = argparse.ArgumentParser(description="优化版足球比赛分析工具")
    parser.add_argument("--searcher", type=str, default="sogou", help="选择搜索引擎 (sogou, sportsdata)")
    parser.add_argument("--optimized", action="store_true", help="使用优化版并发处理")

    args = parser.parse_args()

    analyzer = OptimizedFootballAnalyzer(searcher_name=args.searcher)

    try:
        if args.optimized:
            analyzer.process_all_matches_optimized()
        else:
            print("请使用 --optimized 参数运行优化版本")
            print("示例: python main_optimized.py --optimized")
    except KeyboardInterrupt:
        print("\n用户中断程序")
    except Exception as e:
        print(f"\n程序异常: {e}")
    finally:
        analyzer.cleanup()


if __name__ == "__main__":
    main()