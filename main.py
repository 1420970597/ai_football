#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
足球比赛分析工具
自动获取比赛数据，搜索相关文章，并提取内容，进行AI分析和预测
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
from datetime import datetime, timedelta
from typing import List, Dict, Optional
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


class FootballAnalyzer:
    """足球比赛分析器（集成AI分析功能）"""

    def __init__(self, searcher_name: str = "sogou"):
        self.output_dir = Path("output")
        self.article_dir = self.output_dir / "articles"
        self.analysis_dir = self.output_dir / "analysis"
        self.searcher = get_searcher(searcher_name)
        self.driver = None
        self.current_match_cookies_acquired = False  # 当前比赛是否已获取cookies

        # 验证码识别API配置
        self.captcha_api_url = "http://api.jfbym.com/api/YmServer/customApi"
        self.captcha_token = "FPeil-aOu2nS3DwUQTI_0nDo-ByLtRfRrB47OzYNJYQ"
        self.captcha_type = "30100"

        # 初始化AI客户端
        self.ai_client = AIAnalysisClient()

        # 创建输出目录
        self.article_dir.mkdir(parents=True, exist_ok=True)
        self.analysis_dir.mkdir(parents=True, exist_ok=True)

        # 设置日志
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        self.logger = logging.getLogger(__name__)

    def setup_chrome_driver(self, enable_manual_verification: bool = True) -> bool:
        """设置Chrome浏览器驱动"""
        if not SELENIUM_AVAILABLE:
            print("Selenium未安装，无法使用浏览器功能")
            return False

        try:
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

            # 窗口设置
            if not enable_manual_verification:
                chrome_options.add_argument('--headless')
                chrome_options.add_argument('--window-size=1920,1080')
            else:
                # 手动验证模式下的设置
                chrome_options.add_argument('--start-maximized')
                chrome_options.add_argument('--disable-infobars')
                chrome_options.add_argument('--disable-extensions')

            # 更真实的浏览器特征
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
                "profile.content_settings.plugin_whitelist.adobe-flash-player": 1,
                "profile.content_settings.exceptions.plugins.*,*.per_resource.adobe-flash-player": 1,
                "PluginsAllowedForUrls": "*",
                "DefaultPluginsSetting": 1,
                "profile.managed_default_content_settings.media_stream": 1,
            }

            # 语言设置
            prefs["intl.accept_languages"] = "zh-CN,zh;q=0.9,en;q=0.8"

            chrome_options.add_experimental_option("prefs", prefs)

            # 禁用自动化检测特征
            chrome_options.add_experimental_option("excludeSwitches", [
                "enable-automation",
                "enable-blink-features=AutomationControlled"
            ])

            chrome_options.add_experimental_option('useAutomationExtension', False)

            # 使用多种方式尝试获取ChromeDriver
            driver_service = None

            # 方法1：尝试使用webdriver-manager自动下载（在线模式）
            try:
                print("    尝试自动下载ChromeDriver...")
                service = Service(ChromeDriverManager().install())
                driver_service = service
                print("    [成功] 成功通过webdriver-manager获取ChromeDriver")
            except Exception as e:
                print(f"    [失败] webdriver-manager失败: {e}")

            # 如果方法1失败，尝试方法2：使用系统PATH中的chromedriver
            if not driver_service:
                try:
                    print("    尝试使用系统PATH中的chromedriver...")
                    chromedriver_path = shutil.which("chromedriver")
                    if chromedriver_path:
                        driver_service = Service(chromedriver_path)
                        print(f"    [成功] 找到系统chromedriver: {chromedriver_path}")
                    else:
                        print("    [失败] 系统PATH中未找到chromedriver")
                except Exception as e2:
                    print(f"    [失败] 系统PATH查找失败: {e2}")

            # 如果方法2也失败，尝试方法3：常见的默认安装路径
            if not driver_service:
                try:
                    print("    尝试常见的chromedriver路径...")
                    possible_paths = [
                        r"C:\Program Files (x86)\Google\Chrome\Application\chromedriver.exe",
                        r"C:\Program Files\Google\Chrome\Application\chromedriver.exe",
                        r"C:\chromedriver\chromedriver.exe",
                        "./chromedriver.exe",
                        "./chromedriver"
                    ]

                    for path in possible_paths:
                        if os.path.exists(path):
                            driver_service = Service(path)
                            print(f"    [成功] 找到chromedriver: {path}")
                            break

                    if not driver_service:
                        print("    [失败] 所有路径都未找到chromedriver")
                except Exception as e3:
                    print(f"    [失败] 本地路径查找失败: {e3}")

            # 如果所有方法都失败，提供解决方案
            if not driver_service:
                print("    [失败] 无法获取ChromeDriver")
                print("    [提示] 解决方案:")
                print("       1. 检查网络连接后重试")
                print("       2. 手动下载chromedriver并放到系统PATH中")
                print("       3. 将chromedriver.exe放到项目目录")
                print("       4. 下载地址: https://chromedriver.chromium.org/")
                print("       5. 或者使用 --info-only 模式仅获取文章信息")
                return False

            # 创建Chrome实例
            try:
                self.driver = webdriver.Chrome(service=driver_service, options=chrome_options)

                # 执行反检测脚本
                stealth_script = """
                // 隐藏webdriver特征
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

                // 删除自动化相关属性
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
                    runtime: {
                        onConnect: null,
                        onMessage: null
                    },
                    loadTimes: function() {
                        return {
                            commitLoadTime: 1234567890.123,
                            connectionInfo: 'h2',
                            finishDocumentLoadTime: 1234567890.456,
                            finishLoadTime: 1234567890.789,
                            firstPaintAfterLoadTime: 0,
                            firstPaintTime: 1234567890.111,
                            navigationType: 'Navigation',
                            npnNegotiatedProtocol: 'h2',
                            requestTime: 1234567890.000,
                            startLoadTime: 1234567890.001,
                            wasAlternateProtocolAvailable: false,
                            wasFetchedViaSpdy: true,
                            wasNpnNegotiated: true
                        };
                    },
                    csi: function() {
                        return {
                            onloadT: 1234567890,
                            pageT: 12345.678,
                            startE: 1234567890123,
                            tran: 15
                        };
                    },
                    app: {
                        isInstalled: false,
                        InstallState: {
                            DISABLED: 'disabled',
                            INSTALLED: 'installed',
                            NOT_INSTALLED: 'not_installed'
                        },
                        RunningState: {
                            CANNOT_RUN: 'cannot_run',
                            READY_TO_RUN: 'ready_to_run',
                            RUNNING: 'running'
                        }
                    }
                };

                // 修改权限查询
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({ state: Notification.permission }) :
                        originalQuery(parameters)
                );

                // 修改screen属性
                Object.defineProperty(screen, 'colorDepth', {get: () => 24});
                Object.defineProperty(screen, 'pixelDepth', {get: () => 24});

                // 模拟真实的连接信息
                Object.defineProperty(navigator, 'connection', {
                    get: () => ({
                        effectiveType: '4g',
                        rtt: 50,
                        downlink: 2
                    })
                });

                // 隐藏自动化痕迹
                const getParameter = WebGLRenderingContext.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {
                    if (parameter === 37445) {
                        return 'Intel Inc.';
                    }
                    if (parameter === 37446) {
                        return 'Intel(R) Iris(TM) Graphics 6100';
                    }
                    return getParameter(parameter);
                };
                """

                self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
                    'source': stealth_script
                })

                # 设置页面加载策略
                self.driver.implicitly_wait(10)

                self.manual_verification_enabled = enable_manual_verification
                verification_mode = "支持手动验证" if enable_manual_verification else "自动模式"
                print(f"Chrome浏览器驱动启动成功 ({verification_mode})")
                return True

            except Exception as e:
                print(f"Chrome驱动设置失败: {e}")
                print("请确保已安装Chrome浏览器")
                return False

        except Exception as e:
            print(f"浏览器设置失败: {e}")
            return False

    def acquire_cookies_for_match(self, match_keyword: str) -> bool:
        """为当前比赛获取新的cookies"""
        if not self.driver:
            print("    [失败] 浏览器驱动未初始化")
            return False

        try:
            print(f"    [Cookie] 为比赛 {match_keyword} 获取新的cookies...")

            # 步骤1：访问sogou.com获取cookies
            self.driver.get("https://www.sogou.com")
            time.sleep(3)  # 等待页面加载

            # 获取当前cookies数量
            current_cookies = self.driver.get_cookies()
            sogou_cookies = [c for c in current_cookies if '.sogou.com' in c.get('domain', '')]

            print(f"    [成功] 成功从sogou.com获取到 {len(sogou_cookies)} 个相关cookies")

            # 标记当前比赛已获取cookies
            self.current_match_cookies_acquired = True

            return True

        except Exception as e:
            print(f"    [失败] 获取cookies失败: {e}")
            self.current_match_cookies_acquired = False
            return False

    def clean_old_files(self):
        """清理72小时前的文件夹和比赛JSON文件"""
        current_time = datetime.now()
        cutoff_time = current_time - timedelta(hours=72)

        print("[清理] 开始清理72小时前的旧文件...")
        cleaned_folders = 0
        cleaned_json_files = 0

        # 清理文章文件夹（output/articles/下的match_*文件夹）
        if self.article_dir.exists():
            for folder in self.article_dir.iterdir():
                if folder.is_dir() and folder.name.startswith("match_"):
                    try:
                        # 从文件夹名称中提取时间戳
                        parts = folder.name.split("_")
                        if len(parts) >= 3:
                            timestamp = int(parts[-1])
                            folder_time = datetime.fromtimestamp(timestamp)

                            if folder_time < cutoff_time:
                                shutil.rmtree(folder)
                                cleaned_folders += 1
                                print(f"  删除过期文件夹: {folder.name}")
                    except (ValueError, OSError) as e:
                        print(f"  清理文件夹 {folder.name} 失败: {e}")

        # 清理比赛JSON文件（output/下的场次*.json文件）
        if self.output_dir.exists():
            for json_file in self.output_dir.glob("场次*.json"):
                try:
                    # 获取文件修改时间
                    file_mtime = datetime.fromtimestamp(json_file.stat().st_mtime)

                    if file_mtime < cutoff_time:
                        json_file.unlink()
                        cleaned_json_files += 1
                        print(f"  删除过期比赛文件: {json_file.name}")
                except OSError as e:
                    print(f"  清理文件 {json_file.name} 失败: {e}")

        if cleaned_folders > 0 or cleaned_json_files > 0:
            print("[成功] 清理完成：删除了 {cleaned_folders} 个文章文件夹，{cleaned_json_files} 个比赛JSON文件".format(cleaned_folders=cleaned_folders, cleaned_json_files=cleaned_json_files))
        else:
            print("[完成] 无需清理：没有发现72小时前的旧文件")

    def check_match_already_processed(self, keyword: str) -> bool:
        """检查比赛是否在24小时内已经处理过"""
        current_time = datetime.now()
        cutoff_time = current_time - timedelta(hours=24)

        # 检查文章文件夹
        if self.article_dir.exists():
            for folder in self.article_dir.iterdir():
                if folder.is_dir() and folder.name.startswith("match_"):
                    try:
                        # 检查文件夹名称是否包含相同的比赛关键词
                        parts = folder.name.split("_")
                        if len(parts) >= 3:
                            folder_keyword = "_".join(parts[1:-1])  # 提取关键词部分
                            timestamp = int(parts[-1])
                            folder_time = datetime.fromtimestamp(timestamp)

                            # 检查是否是同一场比赛且在24小时内创建
                            if folder_keyword == keyword and folder_time > cutoff_time:
                                print(f"  [跳过]  跳过比赛 {keyword}：24小时内已处理（文件夹：{folder.name}）")
                                return True
                    except (ValueError, IndexError):
                        continue

        return False

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

    def wait_for_manual_verification(self, max_wait_time: int = 300) -> bool:
        """等待用户手动完成验证"""
        try:
            print("\n" + "="*50)
            print("检测到验证码页面")
            print("请在弹出的浏览器窗口中手动完成验证")
            print("验证步骤：")
            print("   1. 完成验证码验证")
            print("   2. 等待页面跳转到文章内容")
            print("   3. 程序将自动继续")
            print(f"最多等待 {max_wait_time} 秒")
            print("="*50)
        except UnicodeEncodeError:
            print("\n" + "="*50)
            print("检测到验证码页面，请手动完成验证")
            print("="*50)

        start_time = time.time()
        check_count = 0

        while time.time() - start_time < max_wait_time:
            try:
                check_count += 1
                current_title = self.driver.title
                page_source = self.driver.page_source
                current_url = self.driver.current_url

                # 检查验证是否完成的多个条件
                verification_completed = False

                # 1. 不再是验证页面
                verification_indicators = [
                    "搜狗" in current_title and "验证" in page_source,
                    "安全验证" in page_source,
                    "请点击" in page_source and "验证码" in page_source,
                    "security verification" in page_source.lower(),
                    "captcha" in page_source.lower()
                ]

                if not any(verification_indicators):
                    verification_completed = True
                    try:
                        print("检测到验证页面已消失，验证成功！")
                    except UnicodeEncodeError:
                        print("验证成功！")

                # 2. 检查是否有文章内容的标识
                article_indicators = [
                    'js_content' in page_source.lower(),
                    'rich_media' in page_source.lower(),
                    'weixin' in page_source.lower(),
                    'article' in page_source.lower(),
                    'content' in page_source.lower() and len(page_source) > 5000
                ]

                if any(article_indicators):
                    verification_completed = True
                    try:
                        print("检测到文章内容页面，验证成功！")
                    except UnicodeEncodeError:
                        print("验证成功！")

                # 3. URL发生了变化（通常意味着跳转成功）
                if 'weixin.qq.com' in current_url or 'mp.weixin.qq.com' in current_url:
                    verification_completed = True
                    try:
                        print("检测到微信文章URL，验证成功！")
                    except UnicodeEncodeError:
                        print("验证成功！")

                if verification_completed:
                    time.sleep(3)  # 等待页面完全加载
                    return True

                # 定期提示等待状态
                elapsed_time = time.time() - start_time
                remaining_time = max_wait_time - elapsed_time

                if check_count % 15 == 0:  # 每30秒提示一次
                    try:
                        print(f"还在等待验证... 剩余时间: {int(remaining_time)} 秒")
                        print("   提示：如果已完成验证但程序未自动继续，请检查页面是否已跳转到文章内容")
                        if check_count % 30 == 0:  # 每60秒显示当前页面标题
                            print(f"   当前页面: {current_title[:50]}...")
                            print(f"   当前URL: {current_url[:80]}...")
                    except UnicodeEncodeError:
                        print(f"等待中... 剩余: {int(remaining_time)} 秒")

                time.sleep(2)  # 每2秒检查一次

            except Exception as e:
                print(f"等待验证时出错: {e}")
                time.sleep(5)  # 出错后稍长延迟

        try:
            print("验证等待超时")
        except UnicodeEncodeError:
            print("timeout")
        return False

    def find_captcha_element(self):
        """查找验证码图片元素的通用方法（优化版 - 避免误判文章图片）"""
        # 优先级更高的验证码特征选择器
        high_priority_selectors = [
            # 基于ID和class的明确验证码特征
            'img[id*="verify"]',
            'img[class*="verify"]',
            'img[id*="captcha"]',
            'img[class*="captcha"]',
            'img[class*="back-img"]',
            
            # 基于容器的验证码选择器
            '.verify-img img',
            '.captcha-img img',
            '.verify-container img',
            '#verify-img',
            '#captcha-img'
        ]
        
        # 基于src属性的选择器（不包括通用data:image）
        src_based_selectors = [
            'img[src*="captcha"]',
            'img[src*="verify"]'
        ]
        
        # 基于尺寸的选择器（验证码图片通常有特定尺寸）
        size_based_selectors = [
            'img[width="310px"]',
            'img[height="155px"]'
        ]
        
        # 按优先级顺序查找
        all_selectors = high_priority_selectors + src_based_selectors + size_based_selectors
        
        for selector in all_selectors:
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                if elements:
                    # 检查找到的元素是否真的是验证码图片
                    for element in elements:
                        src = element.get_attribute('src')
                        class_name = element.get_attribute('class') or ''
                        element_id = element.get_attribute('id') or ''
                        
                        # 验证是否是验证码图片的条件（更严格的判断）
                        is_captcha = any([
                            'verify' in src.lower() if src else False,
                            'captcha' in src.lower() if src else False,
                            'verify' in class_name.lower(),
                            'captcha' in class_name.lower(),
                            'verify' in element_id.lower(),
                            'captcha' in element_id.lower(),
                            'back-img' in class_name.lower(),  # 特定的验证码类名
                        ])
                        
                        # 如果是基于ID/class的高优先级选择器找到的，直接认为是验证码
                        if selector in high_priority_selectors:
                            is_captcha = True
                        
                        if is_captcha:
                            print(f"    [找到] 验证码元素: {selector}")
                            return element
            except:
                continue
        
        return None

    def capture_captcha_image(self) -> Optional[str]:
        """提取验证码图片的base64编码（优化版）"""
        try:
            captcha_element = self.find_captcha_element()
            
            if not captcha_element:
                print("    [失败] 未找到验证码图片元素")
                return None
            
            # 获取图片的src属性
            src_value = captcha_element.get_attribute('src')
            if not src_value:
                print("    [失败] 验证码图片元素无src属性")
                return None
            
            print(f"    [信息] 图片src类型: {src_value[:50]}...")
            
            # 检查是否是base64格式的图片
            if src_value.startswith('data:image/'):
                # 直接提取base64部分
                if ',base64,' in src_value:
                    base64_data = src_value.split(',base64,')[1]
                    print("    [成功] 直接从src属性提取base64编码")
                    return base64_data
                elif ',' in src_value:
                    # 处理其他格式的data URL
                    base64_data = src_value.split(',')[1]
                    print("    [成功] 从data URL提取base64编码")
                    return base64_data
                else:
                    print("    [失败] data URL格式不正确")
                    return None
            else:
                # 如果不是base64格式，回退到截图方式
                print("    [信息] 图片非base64格式，使用截图方式")
                try:
                    screenshot = captcha_element.screenshot_as_png
                    base64_image = base64.b64encode(screenshot).decode()
                    print("    [成功] 通过截图获取base64编码")
                    return base64_image
                except Exception as e:
                    print(f"    [失败] 截图失败: {e}")
                    return None
            
        except Exception as e:
            print(f"    [失败] 提取验证码图片失败: {e}")
            return None

    def get_verify_msg_content(self) -> str:
        """获取verify-msg元素中【】括号内的内容"""
        try:
            verify_msg_selectors = [
                '[id*="verify-msg"]',
                '[class*="verify-msg"]',
                '.verify-msg',
                '#verify-msg',
                '.verify-text',
                '.captcha-text',
                '.verify-tip'
            ]
            
            full_text = ""
            
            # 先尝试通过选择器查找
            for selector in verify_msg_selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    if elements:
                        content = elements[0].text.strip()
                        if content:
                            full_text = content
                            break
                except:
                    continue
            
            # 如果没找到专门的verify-msg元素，尝试从页面源码中提取
            if not full_text:
                page_source = self.driver.page_source
                import re
                
                # 匹配包含【】的文本
                patterns = [
                    r'请依次点击【([^】]+)】',  # 匹配"请依次点击【xxx】"
                    r'点击【([^】]+)】',      # 匹配"点击【xxx】"
                    r'选择【([^】]+)】',      # 匹配"选择【xxx】"
                    r'【([^】]+)】',         # 通用匹配【xxx】
                ]
                
                for pattern in patterns:
                    matches = re.findall(pattern, page_source)
                    if matches:
                        full_text = f"请依次点击【{matches[0]}】"
                        break
            
            # 提取【】中的内容
            if full_text:
                print(f"    [完整文本] {full_text}")
                
                # 使用正则表达式提取【】中的内容
                import re
                bracket_pattern = r'【([^】]+)】'
                matches = re.findall(bracket_pattern, full_text)
                
                if matches:
                    extracted_content = matches[0]
                    print(f"    [提取内容] 【】中的内容: '{extracted_content}'")
                    return extracted_content
                else:
                    print(f"    [警告] 未找到【】括号，返回完整文本")
                    return full_text
            
            print(f"    [失败] 未找到verify-msg相关内容")
            return ""
            
        except Exception as e:
            print(f"    [失败] 获取verify-msg内容失败: {e}")
            return ""

    def call_captcha_api(self, base64_image: str, verify_msg_content: str) -> tuple[Optional[List[tuple]], int]:
        """调用验证码识别API"""
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
            
            print(f"    [API] 调用验证码识别API，verify-msg内容: {verify_msg_content}")
            
            response = requests.post(
                self.captcha_api_url,
                headers=headers,
                json=data,
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                print(f"    [API] API响应: {result}")
                
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
                        
                        print(f"    [成功] 识别成功，获得坐标: {coordinates}")
                        return coordinates, code
                    else:
                        print("    [失败] API返回数据格式错误")
                        return None, code
                elif code == 10007:
                    print(f"    [换图] 图片识别失败，需要刷新页面: {msg}")
                    return None, code
                else:
                    print(f"    [失败] API识别失败: {msg} (code: {code})")
                    return None, code
            else:
                print(f"    [失败] API请求失败，状态码: {response.status_code}")
                return None, 0
                
        except Exception as e:
            print(f"    [失败] 调用验证码识别API失败: {e}")
            return None, 0

    def click_captcha_coordinates(self, coordinates: List[tuple]) -> bool:
        """按顺序点击验证码坐标（高精度版）"""
        try:
            captcha_element = self.find_captcha_element()
            
            if not captcha_element:
                print("    [失败] 未找到验证码图片元素，无法计算点击位置")
                return False
            
            # 获取图片元素的位置和尺寸
            element_location = captcha_element.location
            element_size = captcha_element.size
            
            print(f"    [位置] 验证码图片位置: {element_location}, 尺寸: {element_size}")
            
            # 确保页面滚动到验证码图片可见
            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", captcha_element)
            time.sleep(1)  # 等待滚动完成
            
            # 获取更精确的元素位置信息
            rect_info = self.driver.execute_script("""
                var element = arguments[0];
                var rect = element.getBoundingClientRect();
                return {
                    'left': rect.left,
                    'top': rect.top,
                    'width': rect.width,
                    'height': rect.height,
                    'scrollX': window.scrollX,
                    'scrollY': window.scrollY
                };
            """, captcha_element)
            
            print(f"    [精确位置] 客户端矩形: {rect_info}")
            
            from selenium.webdriver.common.action_chains import ActionChains
            
            # 按顺序点击每个坐标
            for i, (rel_x, rel_y) in enumerate(coordinates, 1):
                try:
                    print(f"    [点击] 第{i}次点击 - 相对坐标: ({rel_x}, {rel_y})")
                    
                    # 验证坐标是否在合理范围内
                    if rel_x < 0 or rel_y < 0 or rel_x > element_size['width'] or rel_y > element_size['height']:
                        print(f"    [警告] 坐标({rel_x}, {rel_y})超出图片范围({element_size['width']}, {element_size['height']})")
                    
                    # 使用最精确的JavaScript事件模拟
                    click_success = self.driver.execute_script("""
                        var element = arguments[0];
                        var relX = arguments[1];
                        var relY = arguments[2];
                        
                        try {
                            var rect = element.getBoundingClientRect();
                            var clientX = rect.left + relX;
                            var clientY = rect.top + relY;
                            
                            // 创建完整的鼠标事件序列
                            var eventOptions = {
                                'view': window,
                                'bubbles': true,
                                'cancelable': true,
                                'clientX': clientX,
                                'clientY': clientY,
                                'screenX': clientX + window.screenX,
                                'screenY': clientY + window.screenY,
                                'button': 0,
                                'buttons': 1,
                                'detail': 1
                            };
                            
                            // 按照真实用户操作顺序触发事件
                            var events = [
                                new MouseEvent('mouseenter', eventOptions),
                                new MouseEvent('mouseover', eventOptions),
                                new MouseEvent('mousemove', eventOptions),
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
                            
                            // 额外触发可能的自定义事件
                            try {
                                var customClick = new Event('captcha-click', {'bubbles': true});
                                element.dispatchEvent(customClick);
                            } catch(e) {}
                            
                            return success;
                        } catch(e) {
                            return false;
                        }
                    """, captcha_element, rel_x, rel_y)
                    
                    if click_success:
                        print(f"    [成功] JavaScript事件点击成功")
                    else:
                        print(f"    [失败] JavaScript事件点击失败，尝试ActionChains")
                        
                        # 备用方案：ActionChains点击
                        try:
                            action = ActionChains(self.driver)
                            action.move_to_element_with_offset(captcha_element, rel_x, rel_y)
                            action.pause(0.1)  # 短暂停顿
                            action.click()
                            action.perform()
                            print(f"    [备用] ActionChains点击执行")
                        except Exception as e:
                            print(f"    [备用失败] {e}")
                    
                    # 添加真实用户行为模拟
                    delay = random.uniform(0.2, 0.4)  # 增加延迟模拟真实用户
                    print(f"    [延迟] 等待 {delay:.2f} 秒")
                    time.sleep(delay)
                    
                    # 检查点击后是否有视觉反馈
                    try:
                        # 检查元素属性是否有变化
                        current_classes = captcha_element.get_attribute('class')
                        current_style = captcha_element.get_attribute('style')
                        print(f"    [反馈] class: {current_classes}, style: {current_style}")
                        
                        # 检查verify-msg变化
                        time.sleep(0.2)  # 等待页面响应
                        current_msg = self.get_verify_msg_content()
                        print(f"    [状态] 点击后verify-msg: '{current_msg}'")
                    except Exception as e:
                        print(f"    [状态检查] 失败: {e}")
                    
                except Exception as e:
                    print(f"    [失败] 第{i}次点击异常: {e}")
                    return False
            
            print(f"    [完成] 已完成{len(coordinates)}次点击")
            
            # 点击完成后等待验证响应
            print(f"    [等待] 等待验证系统响应...")
            time.sleep(2)  # 增加等待时间
            
            # 检查页面是否有加载或处理的迹象
            try:
                page_title = self.driver.title
                current_url = self.driver.current_url
                print(f"    [页面状态] 标题: {page_title}, URL: {current_url[:80]}...")
            except:
                pass
            
            return True
            
        except Exception as e:
            print(f"    [失败] 点击验证码坐标失败: {e}")
            return False

    def check_verification_success(self) -> bool:
        """检查验证是否成功（增强版）"""
        try:
            # 等待页面响应
            time.sleep(1.5)
            
            print("    [检查] 正在检查验证状态...")
            
            # 方法1：检查verify-msg元素的内容变化
            verify_msg_content = self.get_verify_msg_content()
            print(f"    [verify-msg] 当前内容: '{verify_msg_content}'")
            
            # 成功指示词检查
            success_indicators = [
                "验证成功", "验证通过", "成功", "正确", "通过", "验证完成"
            ]
            
            # 检查是否包含成功指示词
            for indicator in success_indicators:
                if indicator in verify_msg_content.lower():
                    print(f"    [成功] 发现成功指示词: '{indicator}'")
                    return True
            
            # 方法2：检查页面整体内容变化
            page_source = self.driver.page_source
            
            # 检查是否有成功相关的文本
            success_texts = [
                "验证成功", "验证通过", "通过验证", "验证完成",
                "success", "verified", "passed"
            ]
            
            for text in success_texts:
                if text in page_source.lower():
                    print(f"    [成功] 页面中发现成功文本: '{text}'")
                    return True
            
            # 方法3：检查验证码元素是否消失或变化
            try:
                captcha_element = self.find_captcha_element()
                if not captcha_element:
                    print(f"    [成功] 验证码图片已消失")
                    return True
                else:
                    # 检查验证码图片是否变化（可能显示成功状态）
                    src = captcha_element.get_attribute('src')
                    if src and any(success_word in src.lower() for success_word in ['success', 'ok', 'pass']):
                        print(f"    [成功] 验证码图片显示成功状态")
                        return True
            except:
                pass
            
            # 方法4：检查URL变化（验证成功可能会跳转）
            current_url = self.driver.current_url
            print(f"    [URL] 当前URL: {current_url[:100]}...")
            
            # 如果URL包含成功相关参数或跳转到内容页面
            if any(param in current_url.lower() for param in ['success', 'verified', 'weixin.qq.com', 'mp.weixin.qq.com']):
                print(f"    [成功] URL变化表明验证成功")
                return True
            
            # 方法5：检查页面标题变化
            current_title = self.driver.title
            print(f"    [标题] 当前标题: {current_title}")
            
            # 如果标题不再包含验证相关词汇
            verification_title_words = ["验证", "captcha", "verify", "robot"]
            if not any(word in current_title.lower() for word in verification_title_words):
                print(f"    [成功] 页面标题不再包含验证相关词汇")
                return True
            
            # 方法6：检查是否有其他验证完成的视觉指标
            try:
                # 查找可能的成功提示元素
                success_selectors = [
                    '.success', '.verify-success', '#success',
                    '[class*="success"]', '[id*="success"]',
                    '.pass', '.verified', '.complete',
                    '[class*="pass"]', '[class*="verified"]'
                ]
                
                for selector in success_selectors:
                    try:
                        elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                        for elem in elements:
                            if elem.is_displayed():
                                elem_text = elem.text.strip()
                                if elem_text and any(word in elem_text.lower() for word in success_indicators):
                                    print(f"    [成功] 发现成功元素: {selector} - '{elem_text}'")
                                    return True
                    except:
                        continue
            except:
                pass
            
            # 方法7：检查页面是否加载了新内容（表明验证通过）
            try:
                # 查找文章内容相关元素
                content_indicators = [
                    '[id*="js_content"]', '.rich_media_content', 
                    'article', '.article-content', '.content'
                ]
                
                for selector in content_indicators:
                    try:
                        elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                        if elements:
                            content = elements[0].text.strip()
                            if len(content) > 200:  # 如果有足够的内容
                                print(f"    [成功] 检测到文章内容，验证可能已通过")
                                return True
                    except:
                        continue
            except:
                pass
            
            # 方法8：检查验证相关元素是否仍然存在
            verification_still_present = any([
                "验证码" in page_source,
                "captcha" in page_source.lower(),
                "请点击" in page_source and "验证" in page_source,
                "robot" in page_source.lower()
            ])
            
            if not verification_still_present:
                print(f"    [成功] 页面不再显示验证相关内容")
                return True
            
            # 最后检查：尝试查找submit按钮是否可用
            try:
                submit_selectors = [
                    'a[id="submit"]', '#submit', 'button[type="submit"]',
                    '.submit', '[onclick*="submit"]'
                ]
                
                for selector in submit_selectors:
                    try:
                        elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                        if elements:
                            submit_elem = elements[0]
                            if submit_elem.is_enabled() and submit_elem.is_displayed():
                                # 检查submit按钮是否变为可点击状态
                                print(f"    [发现] submit按钮可用，可能验证已完成")
                                return True
                    except:
                        continue
            except:
                pass
            
            print(f"    [失败] 验证未通过，所有检查方法均未发现成功标识")
            return False
            
        except Exception as e:
            print(f"    [失败] 检查验证状态失败: {e}")
            return False

    def click_submit_button(self) -> bool:
        """点击提交按钮"""
        try:
            submit_selectors = [
                'a[id="submit"]',
                '#submit',
                'button[type="submit"]',
                '.submit',
                '[onclick*="submit"]'
            ]
            
            submit_element = None
            for selector in submit_selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    if elements:
                        submit_element = elements[0]
                        break
                except:
                    continue
            
            if submit_element:
                print("    [提交] 点击提交按钮")
                submit_element.click()
                time.sleep(2)  # 等待提交响应
                print("    [成功] 提交按钮已点击")
                return True
            else:
                print("    [失败] 未找到提交按钮")
                return False
                
        except Exception as e:
            print(f"    [失败] 点击提交按钮失败: {e}")
            return False

    def debug_captcha_click(self, coordinates: List[tuple]) -> None:
        """调试验证码点击功能"""
        try:
            print("    [调试] 开始验证码点击调试...")
            
            captcha_element = self.find_captcha_element()
            if not captcha_element:
                print("    [调试] 未找到验证码元素")
                return
            
            # 获取详细信息
            location = captcha_element.location
            size = captcha_element.size
            rect = self.driver.execute_script("""
                var element = arguments[0];
                var rect = element.getBoundingClientRect();
                return {
                    'left': rect.left,
                    'top': rect.top,
                    'width': rect.width,
                    'height': rect.height,
                    'visible': rect.width > 0 && rect.height > 0
                };
            """, captcha_element)
            
            print(f"    [调试] 元素位置: {location}")
            print(f"    [调试] 元素尺寸: {size}")
            print(f"    [调试] 客户端位置: {rect}")
            print(f"    [调试] 元素可见: {rect['visible']}")
            
            # 检查坐标是否在图片范围内
            print(f"    [调试] 检查坐标范围:")
            for i, (x, y) in enumerate(coordinates, 1):
                in_bounds = (0 <= x <= size['width'] and 0 <= y <= size['height'])
                print(f"      坐标{i}: ({x}, {y}) - {'✓在范围内' if in_bounds else '✗超出范围'}")
            
            # 测试是否可以点击元素
            try:
                self.driver.execute_script("arguments[0].click();", captcha_element)
                print(f"    [调试] 直接点击元素: 成功")
            except Exception as e:
                print(f"    [调试] 直接点击元素: 失败 - {e}")
            
        except Exception as e:
            print(f"    [调试] 调试过程出错: {e}")

    def save_debug_screenshot(self, prefix: str) -> str:
        """保存调试截图"""
        try:
            timestamp = int(time.time())
            screenshot_path = f"debug_{prefix}_{timestamp}.png"
            self.driver.save_screenshot(screenshot_path)
            print(f"    [截图] 已保存: {screenshot_path}")
            return screenshot_path
        except Exception as e:
            print(f"    [截图失败] {e}")
            return ""

    def create_independent_driver(self):
        """创建独立的浏览器实例用于并发处理"""
        if not SELENIUM_AVAILABLE:
            print("    [失败] Selenium未安装")
            return None
            
        try:
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

            # 有头模式（显示浏览器窗口）用于调试
            # chrome_options.add_argument('--headless')  # 注释掉无头模式
            chrome_options.add_argument('--window-size=1920,1080')
            chrome_options.add_argument('--start-maximized')  # 最大化窗口便于观察

            # 更真实的浏览器特征
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

            # 使用现有的driver service
            driver_service = None
            try:
                service = Service(ChromeDriverManager().install())
                driver_service = service
            except Exception as e1:
                print(f"    [警告] ChromeDriverManager失败: {e1}")
                # 回退到系统PATH
                try:
                    chromedriver_path = shutil.which("chromedriver")
                    if chromedriver_path:
                        driver_service = Service(chromedriver_path)
                        print(f"    [回退] 使用系统chromedriver: {chromedriver_path}")
                    else:
                        print("    [失败] 系统PATH中未找到chromedriver")
                except Exception as e2:
                    print(f"    [失败] 查找系统chromedriver失败: {e2}")

            if not driver_service:
                print("    [失败] 无法获取ChromeDriver服务")
                return None

            # 创建独立的Chrome实例
            print("    [创建] 正在创建浏览器实例...")
            independent_driver = webdriver.Chrome(service=driver_service, options=chrome_options)
            
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

            independent_driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
                'source': stealth_script
            })
            
            independent_driver.implicitly_wait(10)
            print("    [成功] 浏览器实例创建成功")
            return independent_driver
            
        except Exception as e:
            print(f"    [失败] 创建独立浏览器实例失败: {e}")
            return None

    def extract_article_content_with_driver(self, url: str, keyword: str, driver) -> Dict:
        """使用指定的浏览器实例提取文章内容"""
        if not driver:
            return {
                'success': False,
                'error': '浏览器驱动未初始化',
                'content': '',
                'title': '',
                'text': '',
                'url': url,
                'access_time': datetime.now().isoformat()
            }

        try:
            print(f"        [访问] 正在访问: {url[:60]}...")
            
            # 更自然的访问行为（优化：减少延迟时间）
            import random

            # 随机延迟模拟真实用户（优化：从3-7秒减少到0.5-1.5秒）
            delay = random.uniform(0.5, 1.5)
            time.sleep(delay)

            # 访问目标URL
            driver.get(url)

            # 等待页面加载，使用更智能的等待（优化：减少超时时间）
            try:
                WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                print(f"        [加载] 页面加载完成")
                # 额外等待JavaScript执行（优化：从2-4秒减少到0.5-1秒）
                time.sleep(random.uniform(0.5, 1))
            except:
                print(f"        [警告] 页面加载超时，继续处理")

            # 智能等待页面跳转完成（优化：减少等待时间）
            initial_url = driver.current_url
            
            # 最多等待1.5秒检测URL变化
            for i in range(3):  # 3次 * 0.5秒 = 1.5秒
                time.sleep(0.5)
                current_url = driver.current_url
                if current_url != initial_url:
                    print(f"        [跳转] 检测到页面跳转")
                    time.sleep(0.3)  # 跳转后额外等待0.3秒
                    break
            
            title = driver.title
            page_source = driver.page_source
            
            print(f"        [页面] 标题: {title[:30]}...")
            print(f"        [页面] 内容长度: {len(page_source)} 字符")

            # 使用精确验证页面检测
            if self.is_verification_page_with_driver(title, page_source, driver):
                print(f"        [验证] 检测到验证页面，尝试自动验证码识别...")
                
                # 尝试自动解决验证码（简化版，适用于并发）
                verification_success = self.auto_solve_captcha_with_driver(driver)
                
                if verification_success:
                    print(f"        [验证] 自动验证码识别成功，重新获取内容...")
                    # 重新获取页面信息
                    time.sleep(2)
                    title = driver.title
                    page_source = driver.page_source
                    print(f"        [页面] 验证后标题: {title[:30]}...")
                    print(f"        [页面] 验证后内容长度: {len(page_source)} 字符")
                else:
                    print(f"        [验证] 自动验证码识别失败，跳过此文章")
                    return {
                        'success': False,
                        'error': '验证码处理失败',
                        'content': '',
                        'title': title,
                        'text': '验证码识别失败',
                        'url': url,
                        'access_time': datetime.now().isoformat()
                    }

            # 尝试提取文本内容
            text_content = ""
            try:
                # 优化的文章内容选择器
                selectors = [
                    '[id*="js_content"]',  # 微信公众号主要内容
                    '.rich_media_content',  # 微信公众号
                    '#js_content',  # 微信文章内容
                    '.rich_media_area_primary',  # 微信主要区域
                    'article',
                    '.article-content',
                    '.content',
                    '.post-content',
                    '.text-content',
                    'main'
                ]

                for selector in selectors:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    if elements:
                        text_content = elements[0].text.strip()
                        if len(text_content) > 200:  # 确保获取到有意义的内容
                            print(f"        [内容] 通过选择器 {selector} 提取到 {len(text_content)} 字符")
                            break

                # 如果没有找到有效内容，尝试从body获取
                if len(text_content) < 200:
                    try:
                        body_text = driver.find_element(By.TAG_NAME, "body").text
                        if len(body_text) > len(text_content):
                            text_content = body_text
                            print(f"        [内容] 通过body标签提取到 {len(text_content)} 字符")
                    except:
                        pass

                # 清理文本内容，移除可能导致编码问题的字符
                if text_content:
                    # 过滤掉代理对字符（surrogate pairs）
                    text_content = ''.join(c for c in text_content if ord(c) < 0x10000)
                    # 限制文本长度，避免JSON过大
                    text_content = text_content[:5000]

            except Exception as e:
                print(f"        [错误] 文本提取失败: {e}")
                text_content = f"文本提取失败: {e}"

            # 清理页面标题，避免编码问题
            title = ''.join(c for c in title if ord(c) < 0x10000) if title else ""
            
            success = len(text_content) > 50  # 至少要有50个字符才算成功
            print(f"        [结果] 提取{'成功' if success else '失败'}: {len(text_content)} 字符")

            return {
                'success': success,
                'error': '' if success else '内容太少或提取失败',
                'title': title,
                'text': text_content,
                'url': url,
                'access_time': datetime.now().isoformat()
            }

        except Exception as e:
            print(f"        [异常] 提取过程异常: {e}")
            return {
                'success': False,
                'error': f'文章提取失败: {str(e)}',
                'content': '',
                'title': '',
                'text': '',
                'url': url,
                'access_time': datetime.now().isoformat()
            }

    def auto_solve_captcha_with_driver(self, driver, max_retries: int = 2) -> bool:
        """使用指定driver的自动验证码处理（并发版本）"""
        print(f"        [验证码] 开始自动验证码识别...")
        
        for attempt in range(max_retries):
            try:
                print(f"        [尝试] 第{attempt + 1}/{max_retries}次尝试")
                
                # 1. 获取verify-msg内容
                verify_msg_content = self.get_verify_msg_content_with_driver(driver)
                if not verify_msg_content:
                    print(f"        [失败] 无法获取verify-msg内容")
                    continue
                
                # 2. 获取验证码图片base64
                base64_image = self.capture_captcha_image_with_driver(driver)
                if not base64_image:
                    print(f"        [失败] 无法获取验证码图片")
                    continue
                
                # 3. 调用API获取坐标
                coordinates, api_code = self.call_captcha_api(base64_image, verify_msg_content)
                
                # 如果API返回10007（图片识别失败），刷新页面重新获取
                if api_code == 10007:
                    print(f"        [刷新] 图片识别失败，刷新页面重新获取验证码...")
                    driver.refresh()
                    time.sleep(3)
                    continue
                
                if not coordinates:
                    print(f"        [失败] 验证码API识别失败")
                    continue
                
                print(f"        [坐标] 获得点击坐标: {coordinates}")
                
                # 4. 执行点击
                success = self.click_captcha_coordinates_with_driver(driver, coordinates)
                if not success:
                    print(f"        [失败] 点击操作失败")
                    continue
                
                # 5. 等待并检查验证结果
                time.sleep(2)
                
                # 简化的验证成功检查：只检查verify-msg内容
                current_msg = self.get_verify_msg_content_with_driver(driver)
                if "验证成功" in current_msg or "成功" in current_msg:
                    print(f"        [成功] 验证成功: {current_msg}")
                    
                    # 6. 点击提交按钮
                    self.click_submit_button_with_driver(driver)
                    print(f"        [完成] 验证码自动识别完成！")
                    return True
                else:
                    print(f"        [重试] 验证未通过，当前状态: {current_msg}")
                    time.sleep(1)
                    
            except Exception as e:
                print(f"        [异常] 第{attempt + 1}次尝试异常: {e}")
                time.sleep(1)
        
        print(f"        [失败] 自动验证码识别失败，已尝试{max_retries}次")
        return False

    def get_verify_msg_content_with_driver(self, driver) -> str:
        """使用指定driver获取verify-msg内容"""
        try:
            verify_msg_selectors = [
                '[id*="verify-msg"]',
                '[class*="verify-msg"]',
                '.verify-msg',
                '#verify-msg',
                '.verify-text',
                '.captcha-text',
                '.verify-tip'
            ]
            
            full_text = ""
            
            # 先尝试通过选择器查找
            for selector in verify_msg_selectors:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    if elements:
                        content = elements[0].text.strip()
                        if content:
                            full_text = content
                            break
                except:
                    continue
            
            # 如果没找到专门的verify-msg元素，尝试从页面源码中提取
            if not full_text:
                page_source = driver.page_source
                import re
                
                # 匹配包含【】的文本
                patterns = [
                    r'请依次点击【([^】]+)】',
                    r'点击【([^】]+)】',
                    r'选择【([^】]+)】',
                    r'【([^】]+)】',
                ]
                
                for pattern in patterns:
                    matches = re.findall(pattern, page_source)
                    if matches:
                        full_text = f"请依次点击【{matches[0]}】"
                        break
            
            # 提取【】中的内容
            if full_text:
                print(f"        [完整文本] {full_text}")
                
                import re
                bracket_pattern = r'【([^】]+)】'
                matches = re.findall(bracket_pattern, full_text)
                
                if matches:
                    extracted_content = matches[0]
                    print(f"        [提取内容] 【】中的内容: '{extracted_content}'")
                    return extracted_content
                else:
                    print(f"        [警告] 未找到【】括号，返回完整文本")
                    return full_text
            
            print(f"        [失败] 未找到verify-msg相关内容")
            return ""
            
        except Exception as e:
            print(f"        [失败] 获取verify-msg内容失败: {e}")
            return ""

    def capture_captcha_image_with_driver(self, driver) -> Optional[str]:
        """使用指定driver提取验证码图片的base64编码"""
        try:
            captcha_element = self.find_captcha_element_with_driver(driver)
            
            if not captcha_element:
                print(f"        [失败] 未找到验证码图片元素")
                return None
            
            # 获取图片的src属性
            src_value = captcha_element.get_attribute('src')
            if not src_value:
                print(f"        [失败] 验证码图片元素无src属性")
                return None
            
            print(f"        [信息] 图片src类型: {src_value[:50]}...")
            
            # 检查是否是base64格式的图片
            if src_value.startswith('data:image/'):
                # 直接提取base64部分
                if ',base64,' in src_value:
                    base64_data = src_value.split(',base64,')[1]
                    print(f"        [成功] 直接从src属性提取base64编码")
                    return base64_data
                elif ',' in src_value:
                    # 处理其他格式的data URL
                    base64_data = src_value.split(',')[1]
                    print(f"        [成功] 从data URL提取base64编码")
                    return base64_data
                else:
                    print(f"        [失败] data URL格式不正确")
                    return None
            else:
                # 如果不是base64格式，回退到截图方式
                print(f"        [信息] 图片非base64格式，使用截图方式")
                try:
                    screenshot = captcha_element.screenshot_as_png
                    base64_image = base64.b64encode(screenshot).decode()
                    print(f"        [成功] 通过截图获取base64编码")
                    return base64_image
                except Exception as e:
                    print(f"        [失败] 截图失败: {e}")
                    return None
            
        except Exception as e:
            print(f"        [失败] 提取验证码图片失败: {e}")
            return None

    def find_captcha_element_with_driver(self, driver):
        """使用指定driver查找验证码图片元素"""
        # 优先级更高的验证码特征选择器
        high_priority_selectors = [
            'img[id*="verify"]',
            'img[class*="verify"]',
            'img[id*="captcha"]',
            'img[class*="captcha"]',
            'img[class*="back-img"]',
            '.verify-img img',
            '.captcha-img img',
            '.verify-container img',
            '#verify-img',
            '#captcha-img'
        ]
        
        # 基于src属性的选择器
        src_based_selectors = [
            'img[src*="captcha"]',
            'img[src*="verify"]'
        ]
        
        # 基于尺寸的选择器
        size_based_selectors = [
            'img[width="310px"]',
            'img[height="155px"]'
        ]
        
        # 按优先级顺序查找
        all_selectors = high_priority_selectors + src_based_selectors + size_based_selectors
        
        for selector in all_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if elements:
                    # 检查找到的元素是否真的是验证码图片
                    for element in elements:
                        src = element.get_attribute('src')
                        class_name = element.get_attribute('class') or ''
                        element_id = element.get_attribute('id') or ''
                        
                        # 验证是否是验证码图片的条件
                        is_captcha = any([
                            'verify' in src.lower() if src else False,
                            'captcha' in src.lower() if src else False,
                            'verify' in class_name.lower(),
                            'captcha' in class_name.lower(),
                            'verify' in element_id.lower(),
                            'captcha' in element_id.lower(),
                            'back-img' in class_name.lower(),
                        ])
                        
                        # 如果是基于ID/class的高优先级选择器找到的，直接认为是验证码
                        if selector in high_priority_selectors:
                            is_captcha = True
                        
                        if is_captcha:
                            print(f"        [找到] 验证码元素: {selector}")
                            return element
            except:
                continue
        
        return None

    def click_captcha_coordinates_with_driver(self, driver, coordinates: List[tuple]) -> bool:
        """使用指定driver点击验证码坐标"""
        try:
            captcha_element = self.find_captcha_element_with_driver(driver)
            
            if not captcha_element:
                print(f"        [失败] 未找到验证码图片元素，无法计算点击位置")
                return False
            
            # 确保页面滚动到验证码图片可见
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", captcha_element)
            time.sleep(1)
            
            from selenium.webdriver.common.action_chains import ActionChains
            
            # 按顺序点击每个坐标
            for i, (rel_x, rel_y) in enumerate(coordinates, 1):
                try:
                    print(f"        [点击] 第{i}次点击 - 相对坐标: ({rel_x}, {rel_y})")
                    
                    # 使用JavaScript事件模拟点击
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
                        print(f"        [成功] JavaScript事件点击成功")
                    else:
                        print(f"        [失败] JavaScript事件点击失败")
                    
                    # 添加延迟模拟真实用户
                    delay = random.uniform(0.2, 0.4)
                    time.sleep(delay)
                    
                except Exception as e:
                    print(f"        [失败] 第{i}次点击异常: {e}")
                    return False
            
            print(f"        [完成] 已完成{len(coordinates)}次点击")
            time.sleep(1)
            return True
            
        except Exception as e:
            print(f"        [失败] 点击验证码坐标失败: {e}")
            return False

    def click_submit_button_with_driver(self, driver) -> bool:
        """使用指定driver点击提交按钮"""
        try:
            submit_selectors = [
                'a[id="submit"]',
                '#submit',
                'button[type="submit"]',
                '.submit',
                '[onclick*="submit"]'
            ]
            
            submit_element = None
            for selector in submit_selectors:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    if elements:
                        submit_element = elements[0]
                        break
                except:
                    continue
            
            if submit_element:
                print(f"        [提交] 点击提交按钮")
                submit_element.click()
                time.sleep(2)
                print(f"        [成功] 提交按钮已点击")
                return True
            else:
                print(f"        [失败] 未找到提交按钮")
                return False
                
        except Exception as e:
            print(f"        [失败] 点击提交按钮失败: {e}")
            return False

    def is_verification_page_with_driver(self, title: str, page_source: str, driver) -> bool:
        """使用指定driver的验证页面检测"""
        # 检查明确的验证页面标识
        strong_indicators = [
            "搜狗" in title and "验证" in page_source,
            "安全验证" in page_source,
            "请点击" in page_source and "验证码" in page_source,
            "security verification" in page_source.lower(),
            "滑动验证" in page_source
        ]
        
        if any(strong_indicators):
            return True
        
        # 弱指示器需要组合判断
        current_url = driver.current_url
        if "sogou.com" in current_url:
            weak_indicators = [
                "captcha" in page_source.lower(),
                "robot" in page_source.lower()
            ]
            if any(weak_indicators) and len(page_source) < 10000:
                return True
        
        return False

    def extract_articles_concurrently(self, articles: List[Dict], keyword: str, max_workers: int = 3) -> List[Dict]:
        """并发提取文章内容（复用浏览器实例）"""
        print(f"  [并发] 使用{max_workers}个浏览器进程并发提取 {len(articles)} 篇文章...")
        
        # 预先创建浏览器实例池
        print(f"  [初始化] 正在创建{max_workers}个浏览器实例...")
        browser_pool = []
        for i in range(max_workers):
            driver = self.create_independent_driver()
            if driver:
                browser_pool.append(driver)
                print(f"    [成功] 浏览器实例 {i+1} 创建成功")
            else:
                print(f"    [失败] 浏览器实例 {i+1} 创建失败")
        
        if not browser_pool:
            print("  [错误] 没有可用的浏览器实例")
            return []
        
        print(f"  [就绪] 浏览器池创建完成，共{len(browser_pool)}个实例")
        
        def extract_worker(article_with_index):
            """工作线程函数"""
            i, article = article_with_index
            # 根据线程ID选择浏览器实例（循环使用）
            driver_index = (i - 1) % len(browser_pool)
            driver = browser_pool[driver_index]
            
            try:
                print(f"    [线程{i}] 开始处理文章 {article.get('title', '无标题')[:30]}...")
                print(f"    [线程{i}] 使用浏览器实例 {driver_index + 1}")
                
                if not driver:
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
                
                print(f"    [线程{i}] 浏览器创建成功，开始提取内容...")
                
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
                
                # 提取文章内容
                content_data = self.extract_article_content_with_driver(url, keyword, driver)
                
                # 优化content_data，移除无用的HTML内容
                optimized_content_data = {
                    'success': content_data.get('success', False),
                    'title': content_data.get('title', ''),
                    'text': content_data.get('text', ''),
                    'url': content_data.get('url', ''),
                    'access_time': content_data.get('access_time', ''),
                    'error': content_data.get('error', '')
                }
                
                success_status = "成功" if optimized_content_data.get('success') else "失败"
                print(f"    [线程{i}] 内容提取{success_status}")
                
                return i, {
                    'article_info': article,
                    'content_data': optimized_content_data
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
                # 浏览器实例在池中，不需要关闭，会被复用
                print(f"    [线程{i}] 浏览器实例{driver_index + 1}处理完成，返回池中供复用")
        
        results = []
        successful_extractions = 0
        blocked_by_antibot = 0
        
        # 使用ThreadPoolExecutor进行并发处理
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 创建任务列表，包含索引和文章数据
            tasks = [(i, article) for i, article in enumerate(articles, 1)]
            
            # 提交所有任务
            future_to_article = {
                executor.submit(extract_worker, task): task
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
                    if result['content_data'].get('success'):
                        successful_extractions += 1
                    elif '验证' in result['content_data'].get('error', ''):
                        blocked_by_antibot += 1
                    
                    # 显示进度和即时结果
                    content_data = result['content_data']
                    success = content_data.get('success', False)
                    text_length = len(content_data.get('text', ''))
                    status = "成功" if success else "失败"
                    
                    print(f"    [进度] 已完成 {completed_count}/{len(articles)} 篇文章提取")
                    print(f"    [即时结果] 文章{i}: {status}, 内容长度: {text_length} 字符")
                    
                except Exception as e:
                    print(f"    [异常] 线程执行异常: {e}")
        
        # 按原始顺序排序结果
        results.sort(key=lambda x: x[0])
        
        print(f"  [统计] 并发提取统计:")
        print(f"    [成功] 成功提取: {successful_extractions} 篇")
        print(f"    [拦截] 验证页面拦截: {blocked_by_antibot} 篇")
        print(f"    [失败] 其他错误: {len(articles) - successful_extractions - blocked_by_antibot} 篇")
        
        if successful_extractions > 0:
            success_rate = (successful_extractions / len(articles)) * 100
            print(f"    [成功率] 成功率: {success_rate:.1f}%")
        
        # 浏览器池管理提示
        print(f"\n  [浏览器池] 处理完成，{len(browser_pool)}个浏览器实例保持打开状态")
        print(f"  [提示] 浏览器窗口将保持打开以供后续复用")
        print(f"  [提示] 如需关闭，请手动关闭浏览器窗口")
        print(f"\n  [重要] 并发提取完成！即将开始JSON文件保存...")
        
        return [result[1] for result in results]

    def is_verification_page(self, title: str, page_source: str) -> bool:
        """更精确的验证页面检测"""
        # 首先检查明确的验证页面标识（最可靠的方法）
        strong_indicators = [
            "搜狗" in title and "验证" in page_source,
            "安全验证" in page_source,
            "请点击" in page_source and "验证码" in page_source,
            "security verification" in page_source.lower(),
            "滑动验证" in page_source
        ]
        
        if any(strong_indicators):
            print(f"    [检测] 发现强验证指示器")
            return True
        
        # 检查是否有验证码图片元素（结合其他条件）
        current_url = self.driver.current_url
        has_captcha_element = False
        
        try:
            captcha_element = self.find_captcha_element()
            if captcha_element:
                has_captcha_element = True
                print("    [检测] 发现验证码图片元素")
        except:
            pass
        
        # 只有在搜狗域名且发现验证码元素时才进一步判断
        if has_captcha_element and "sogou.com" in current_url:
            # 检查页面内容长度，验证页面通常比较简单
            if len(page_source) < 15000:  # 增加阈值，避免误判
                print(f"    [检测] 搜狗域名+验证码元素+简单页面，判断为验证页面")
                return True
        
        # 弱指示器需要组合判断
        weak_indicators = [
            "captcha" in page_source.lower(),
            "robot" in page_source.lower()
        ]
        
        # 只有在URL包含sogou且存在弱指示器时才判断为验证页面
        if "sogou.com" in current_url and any(weak_indicators):
            # 进一步检查页面内容长度，验证页面通常内容较少
            if len(page_source) < 10000:  # 验证页面通常比较简单
                print(f"    [检测] 搜狗域名+弱指示器+简单页面，判断为验证页面")
                return True
        
        return False

    def auto_solve_captcha(self, max_retries: int = 3) -> bool:
        """自动解决验证码（简化版）"""
        print("    [验证码] 开始自动验证码识别...")
        
        for attempt in range(max_retries):
            try:
                print(f"    [尝试] 第{attempt + 1}/{max_retries}次尝试")
                
                # 1. 获取verify-msg内容
                verify_msg_content = self.get_verify_msg_content()
                if not verify_msg_content:
                    print("    [失败] 无法获取verify-msg内容")
                    continue
                
                # 2. 获取验证码图片base64
                base64_image = self.capture_captcha_image()
                if not base64_image:
                    print("    [失败] 无法获取验证码图片")
                    continue
                
                # 3. 调用API获取坐标
                coordinates, api_code = self.call_captcha_api(base64_image, verify_msg_content)
                
                # 如果API返回10007（图片识别失败），刷新页面重新获取
                if api_code == 10007:
                    print("    [刷新] 图片识别失败，刷新页面重新获取验证码...")
                    self.driver.refresh()
                    time.sleep(3)  # 等待页面加载
                    continue
                
                if not coordinates:
                    print("    [失败] 验证码API识别失败")
                    continue
                
                print(f"    [坐标] 获得点击坐标: {coordinates}")
                
                # 4. 执行点击
                success = self.click_captcha_coordinates(coordinates)
                if not success:
                    print("    [失败] 点击操作失败")
                    continue
                
                # 5. 等待并检查验证结果
                time.sleep(2)
                
                # 简化的验证成功检查：只检查verify-msg内容
                current_msg = self.get_verify_msg_content()
                if "验证成功" in current_msg or "成功" in current_msg:
                    print(f"    [成功] 验证成功: {current_msg}")
                    
                    # 6. 点击提交按钮
                    self.click_submit_button()
                    print("    [完成] 验证码自动识别完成！")
                    return True
                else:
                    print(f"    [重试] 验证未通过，当前状态: {current_msg}")
                    time.sleep(1)
                    
            except Exception as e:
                print(f"    [异常] 第{attempt + 1}次尝试异常: {e}")
                time.sleep(1)
        
        print(f"    [失败] 自动验证码识别失败，已尝试{max_retries}次")
        return False

    def extract_article_content(self, url: str, match_keyword: str = "") -> Dict:
        """提取文章内容，支持手动验证"""
        if not self.driver:
            return {
                'success': False,
                'error': '浏览器驱动未初始化',
                'content': '',
                'title': '',
                'text': ''
            }

        # 如果当前比赛还没有获取cookies，先获取
        if not self.current_match_cookies_acquired and match_keyword:
            print(f"    [检测] 检测到新比赛，正在获取cookies...")
            if not self.acquire_cookies_for_match(match_keyword):
                return {
                    'success': False,
                    'error': 'cookies获取失败',
                    'content': '',
                    'title': '',
                    'text': '',
                    'url': url,
                    'access_time': datetime.now().isoformat()
                }

        try:
            print(f"    访问链接: {url}")

            # 更自然的访问行为（优化：减少延迟时间）
            import random

            # 随机延迟模拟真实用户（优化：从3-7秒减少到1-3秒）
            delay = random.uniform(1, 3)
            print(f"    等待 {delay:.1f} 秒...")
            time.sleep(delay)

            # 访问目标URL
            self.driver.get(url)

            # 等待页面加载，使用更智能的等待（优化：减少超时时间）
            try:
                WebDriverWait(self.driver, 8).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                # 额外等待JavaScript执行（优化：从2-4秒减少到1-2秒）
                time.sleep(random.uniform(1, 2))
            except:
                print("    页面加载超时，继续尝试...")

            # 检查是否遇到验证码页面
            # 智能等待页面跳转完成（优化：检测URL变化而非固定等待）
            initial_url = self.driver.current_url
            print("    [等待] 检测页面跳转...")
            
            # 最多等待3秒检测URL变化
            for i in range(6):  # 6次 * 0.5秒 = 3秒
                time.sleep(0.5)
                current_url = self.driver.current_url
                if current_url != initial_url:
                    print(f"    [跳转] 检测到页面跳转: {current_url[:80]}...")
                    time.sleep(0.5)  # 跳转后额外等待0.5秒
                    break
            else:
                print("    [无跳转] 页面无跳转或跳转完成")
            
            title = self.driver.title
            page_source = self.driver.page_source

            # 使用新的精确验证页面检测
            if self.is_verification_page(title, page_source):
                # 如果启用了手动验证模式，首先尝试自动验证码识别
                if hasattr(self, 'manual_verification_enabled') and self.manual_verification_enabled:
                    print("    检测到验证页面，尝试自动验证码识别...")
                    
                    # 尝试自动解决验证码
                    auto_success = self.auto_solve_captcha()
                    
                    if auto_success:
                        # 自动识别成功，重新获取页面信息
                        time.sleep(3)  # 等待页面完全加载
                        title = self.driver.title
                        page_source = self.driver.page_source
                        print("    自动验证码识别成功，继续提取内容...")
                    else:
                        print("    自动验证码识别失败，转为手动验证模式...")
                        # 自动识别失败，回退到手动验证
                        if self.wait_for_manual_verification():
                            # 手动验证成功，重新获取页面信息
                            title = self.driver.title
                            page_source = self.driver.page_source
                            print("    手动验证完成，继续提取内容...")
                        else:
                            return {
                                'success': False,
                                'error': '自动验证码识别失败且手动验证超时',
                                'content': '',
                                'title': title,
                                'text': '验证超时',
                                'url': url,
                                'access_time': datetime.now().isoformat()
                            }
                else:
                    print("    检测到搜狗反爬虫验证页面，跳过此链接")
                    return {
                        'success': False,
                        'error': '遇到搜狗反爬虫验证，无法获取内容',
                        'content': '',
                        'title': title,
                        'text': '需要验证码验证',
                        'url': url,
                        'access_time': datetime.now().isoformat()
                    }

            # 获取页面的HTML
            html_content = self.driver.page_source

            # 尝试提取文本内容
            text_content = ""
            try:
                # 优化的文章内容选择器
                selectors = [
                    '[id*="js_content"]',  # 微信公众号主要内容
                    '.rich_media_content',  # 微信公众号
                    '#js_content',  # 微信文章内容
                    '.rich_media_area_primary',  # 微信主要区域
                    'article',
                    '.article-content',
                    '.content',
                    '.post-content',
                    '.text-content',
                    'main'
                ]

                for selector in selectors:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    if elements:
                        text_content = elements[0].text.strip()
                        if len(text_content) > 200:  # 确保获取到有意义的内容
                            break

                # 如果没有找到有效内容，尝试从body获取
                if len(text_content) < 200:
                    try:
                        body_text = self.driver.find_element(By.TAG_NAME, "body").text
                        if len(body_text) > len(text_content):
                            text_content = body_text
                    except:
                        pass

                # 清理文本内容，移除可能导致编码问题的字符
                if text_content:
                    # 过滤掉代理对字符（surrogate pairs）
                    text_content = ''.join(c for c in text_content if ord(c) < 0x10000)
                    # 限制文本长度，避免JSON过大
                    text_content = text_content[:5000]

            except Exception as e:
                print(f"    提取文本失败: {e}")
                text_content = "文本提取失败"

            # 清理页面标题，避免编码问题
            title = ''.join(c for c in title if ord(c) < 0x10000) if title else ""

            return {
                'success': True,
                'error': '',
                'title': title,
                'text': text_content,
                'url': url,
                'access_time': datetime.now().isoformat()
            }

        except TimeoutException:
            return {
                'success': False,
                'error': '页面加载超时',
                'content': '',
                'title': '',
                'text': '',
                'url': url
            }
        except WebDriverException as e:
            return {
                'success': False,
                'error': f'浏览器错误: {str(e)}',
                'content': '',
                'title': '',
                'text': '',
                'url': url
            }
        except Exception as e:
            return {
                'success': False,
                'error': f'未知错误: {str(e)}',
                'content': '',
                'title': '',
                'text': '',
                'url': url
            }

    def save_match_articles(self, match_info: Dict, articles: List[Dict], keyword: str, extract_content: bool = True):
        """保存比赛相关文章"""
        # 清理关键词，确保文件夹名称安全
        safe_keyword = ''.join(c for c in keyword if c.isalnum() or c in 'vs')
        safe_keyword = safe_keyword[:50]  # 限制长度

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
                    'content_extraction': extract_content
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

        # 只有在启用内容提取时才尝试提取内容
        if extract_content and articles:
            print(f"  开始提取 {len(articles)} 篇文章的详细内容...")
            
            # 使用并发方式提取文章内容（3个浏览器进程）
            extraction_results = self.extract_articles_concurrently(articles, keyword, max_workers=3)
            
            print(f"  [调试] 并发提取完成，返回{len(extraction_results)}个结果")
            for idx, result in enumerate(extraction_results):
                content_data = result.get('content_data', {})
                print(f"    结果{idx+1}: 成功={content_data.get('success', False)}, 内容长度={len(content_data.get('text', ''))}")
            
            # 保存文章数据并统计结果
            successful_extractions = 0
            blocked_by_antibot = 0
            
            print(f"  [保存] 开始保存{len(extraction_results)}篇文章到JSON文件...")
            
            for i, result in enumerate(extraction_results, 1):
                print(f"  [保存循环] 处理第{i}篇文章...")
                article_info = result.get('article_info', {})
                content_data = result.get('content_data', {})
                
                # 保存文章数据（仅保存JSON，不保存HTML）
                article_file = match_folder / f"article_{i:03d}.json"
                combined_data = {
                    'article_info': article_info,
                    'content_data': content_data
                }

                try:
                    print(f"    [保存] 正在保存文章 {i} 到: {article_file}")
                    print(f"    [调试] 文章标题: {article_info.get('title', '无标题')[:50]}...")
                    print(f"    [调试] 内容长度: {len(content_data.get('text', ''))} 字符")
                    print(f"    [调试] 成功状态: {content_data.get('success', False)}")
                    
                    # 确保目录存在
                    article_file.parent.mkdir(parents=True, exist_ok=True)
                    print(f"    [调试] 目录创建成功: {article_file.parent}")
                    
                    with open(article_file, 'w', encoding='utf-8', errors='ignore') as f:
                        json.dump(combined_data, f, ensure_ascii=False, indent=2, cls=DateTimeEncoder)
                    
                    # 验证文件是否真的被创建
                    if article_file.exists():
                        file_size = article_file.stat().st_size
                        print(f"    [成功] 文章 {i} 保存成功，文件大小: {file_size} 字节")
                    else:
                        print(f"    [警告] 文章 {i} 文件未找到，可能保存失败")
                        
                except Exception as e:
                    print(f"    [失败] 保存文章 {i} 失败: {e}")
                    print(f"    [调试] 错误类型: {type(e).__name__}")
                    import traceback
                    print(f"    [调试] 错误详情: {traceback.format_exc()}")

                # 统计结果
                if content_data.get('success'):
                    successful_extractions += 1
                elif '验证码' in content_data.get('error', '') or '验证' in content_data.get('error', ''):
                    blocked_by_antibot += 1

            # 输出统计结果
            print(f"  [统计] 并发内容提取统计:")
            print(f"    [成功] 成功提取: {successful_extractions} 篇")
            print(f"    [拦截] 验证页面拦截: {blocked_by_antibot} 篇")
            print(f"    [失败] 其他错误: {len(articles) - successful_extractions - blocked_by_antibot} 篇")

            if successful_extractions > 0:
                success_rate = (successful_extractions / len(articles)) * 100
                print(f"    [成功率] 成功率: {success_rate:.1f}%")

            if blocked_by_antibot > 0:
                print(f"  [提示] 关于验证页面拦截的说明：")
                print(f"     1. 文章基本信息已完整保存（标题、摘要、发布时间、链接）")
                print(f"     2. 并发模式暂不支持验证码处理，但大幅提升处理速度")
                print(f"     3. 可直接点击保存的链接手动访问获取完整内容")
                print(f"     4. 如需验证码处理，可使用非并发模式")

        elif not extract_content:
            print(f"  仅保存文章信息，跳过内容提取")

        print(f"  比赛 {keyword} 的所有文章已保存到: {match_folder}")

    def create_analysis_prompt(self, article_info: Dict, content_data: Dict) -> str:
        """创建分析提示词"""
        title = article_info.get('title', '无标题')
        summary = article_info.get('summary', '无摘要')
        text_content = content_data.get('text', '无内容')

        # 限制内容长度
        max_length = ANALYSIS_CONFIG["max_content_length"]
        if len(text_content) > max_length:
            text_content = text_content[:max_length] + "..."

        return f"""
请分析以下足球相关文章，提取其中的比赛信息和分析结果：

文章标题：{title}
文章摘要：{summary}
文章内容：{text_content}

请按照以下JSON格式回答：
{{
    "matches": [
        {{
            "home_team": "主队名称",
            "away_team": "客队名称",
            "prediction": "比分预测（如2-1、1-0等）",
            "result_prediction": "结果预测（主胜/平局/客胜）",
            "confidence": "预测置信度（1-10分）",
            "analysis": "详细分析原因"
        }}
    ],
    "summary": "文章整体分析总结"
}}

请确保回答是有效的JSON格式，用中文回答。如果文章中没有明确的比赛信息，matches数组可以为空。
"""

    def analyze_single_article(self, article_info: Dict, content_data: Dict) -> Optional[Dict]:
        """分析单篇文章"""
        title = article_info.get('title', '无标题')

        # 创建分析提示词
        prompt = self.create_analysis_prompt(article_info, content_data)

        messages = [{"role": "user", "content": prompt}]

        result = self.ai_client.chat_completion(messages)

        if result:
            try:
                analysis_json = json.loads(result['content'])

                return {
                    'article_info': {
                        'title': title,
                        'summary': article_info.get('summary', ''),
                        'url': article_info.get('url', ''),
                        'publish_time': article_info.get('publish_time', '')
                    },
                    'analysis_result': analysis_json,
                    'raw_response': result['content'],
                    'usage': result.get('usage', {}),
                    'analysis_time': datetime.now().isoformat()
                }

            except json.JSONDecodeError:
                self.logger.warning(f"JSON解析失败，保存原始响应: {title}")
                return {
                    'article_info': {
                        'title': title,
                        'summary': article_info.get('summary', ''),
                        'url': article_info.get('url', ''),
                        'publish_time': article_info.get('publish_time', '')
                    },
                    'analysis_result': {'raw_text': result['content']},
                    'raw_response': result['content'],
                    'usage': result.get('usage', {}),
                    'analysis_time': datetime.now().isoformat()
                }

        return None

    def analyze_articles_multithreaded(self, articles: List[Dict], max_workers: int = 30) -> List[Dict]:
        """使用多线程分析文章列表"""
        print(f"  [AI] 使用{max_workers}个线程并发分析 {len(articles)} 篇文章...")
        
        def analyze_article_worker(article_with_index):
            """线程工作函数"""
            j, article = article_with_index
            try:
                # 创建文章数据结构用于AI分析
                article_data = {
                    'article_info': article,
                    'content_data': {
                        'success': True,
                        'text': article.get('summary', '') + ' ' + article.get('title', ''),
                        'title': article.get('title', ''),
                        'url': article.get('url', '')
                    }
                }

                # 进行AI分析
                analysis_result = self.analyze_single_article(article, article_data['content_data'])
                if analysis_result:
                    print(f"      [成功] 文章 {j}/{len(articles)} 分析成功: {article.get('title', '无标题')[:30]}...")
                    return analysis_result
                else:
                    print(f"      [失败] 文章 {j}/{len(articles)} 分析失败: {article.get('title', '无标题')[:30]}...")
                    return None
                    
            except Exception as e:
                print(f"      [异常] 文章 {j}/{len(articles)} 分析异常: {e}")
                return None
        
        match_analyses = []
        
        # 使用ThreadPoolExecutor进行并发分析
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 创建任务列表，包含索引和文章数据
            tasks = [(j, article) for j, article in enumerate(articles, 1)]
            
            # 提交所有任务
            future_to_article = {
                executor.submit(analyze_article_worker, task): task 
                for task in tasks
            }
            
            # 收集结果
            completed_count = 0
            for future in as_completed(future_to_article):
                completed_count += 1
                try:
                    result = future.result()
                    if result:
                        match_analyses.append(result)
                    
                    # 显示进度
                    print(f"    [进度] 已完成 {completed_count}/{len(articles)} 篇文章分析")
                    
                except Exception as e:
                    print(f"    [异常] 线程执行异常: {e}")
        
        print(f"  [完成] 多线程分析完成，成功分析 {len(match_analyses)}/{len(articles)} 篇文章")
        return match_analyses

    def check_match_consistency(self, analyses: List[Dict]) -> Dict[str, Dict]:
        """检查比赛结果的一致性"""
        self.logger.info("开始检查比赛结果一致性...")

        # 按比赛分组分析结果
        match_analyses = defaultdict(list)

        for analysis in analyses:
            result = analysis.get('analysis_result', {})
            if isinstance(result, dict) and 'matches' in result:
                for match in result['matches']:
                    if isinstance(match, dict):
                        home = match.get('home_team', '').strip()
                        away = match.get('away_team', '').strip()
                        if home and away:
                            match_key = f"{home}vs{away}".replace(' ', '')
                            match_analyses[match_key].append({
                                'analysis': analysis,
                                'match_data': match,
                                'result_prediction': match.get('result_prediction', ''),
                                'confidence': match.get('confidence', 5)
                            })

        # 计算每场比赛的一致性
        consistency_results = {}

        for match_key, predictions in match_analyses.items():
            if len(predictions) < 2:  # 至少需要2个预测才能计算一致性
                continue

            # 统计预测结果
            result_counts = Counter([p['result_prediction'] for p in predictions if p['result_prediction']])

            if not result_counts:
                continue

            total_predictions = len(predictions)
            most_common_result, most_common_count = result_counts.most_common(1)[0]

            # 计算一致性百分比
            consistency_percentage = (most_common_count / total_predictions) * 100

            # 计算平均置信度
            confidences = [p['confidence'] for p in predictions if isinstance(p['confidence'], (int, float))]
            avg_confidence = statistics.mean(confidences) if confidences else 5

            consistency_results[match_key] = {
                'total_predictions': total_predictions,
                'most_common_result': most_common_result,
                'consistency_percentage': consistency_percentage,
                'average_confidence': avg_confidence,
                'all_predictions': predictions
            }

        return consistency_results

    def generate_wechat_post(self, match_data: Dict, consistency_data: Dict, all_analyses: List[Dict]) -> str:
        """生成微信公众号推文"""

        # 提取比赛基本信息
        basic_info = match_data.get('基本信息', {})
        home_team = basic_info.get('主队名称', '主队')
        away_team = basic_info.get('客队名称', '客队')
        match_time = basic_info.get('比赛时间', '未知时间')
        league_name = basic_info.get('联赛名称', '足球比赛')

        # 提取赔率信息
        odds_info = match_data.get('赔率信息', {})
        had_odds = odds_info.get('胜平负格式', '')

        # 生成综合分析内容
        analysis_summary = []
        for analysis in all_analyses:
            result = analysis.get('analysis_result', {})
            summary = result.get('summary', '')
            if summary:
                analysis_summary.append(summary)

        combined_analysis = "\\n".join(analysis_summary[:3])  # 取前3篇文章的分析

        # 准备一致性信息
        consistency_info = ""
        if consistency_data:
            match_key = list(consistency_data.keys())[0]
            consistency = consistency_data[match_key]
            consistency_info = f"预测一致性：{consistency['consistency_percentage']:.1f}%，预测结果：{consistency['most_common_result']}"

        prompt = f"""
请根据以下信息生成一篇微信公众号足球比赛预测推文：

比赛信息：
- 主队：{home_team}
- 客队：{away_team}
- 比赛时间：{match_time}
- 联赛：{league_name}
- 官方赔率：{had_odds}

文章分析汇总：
{combined_analysis}

一致性分析：
{consistency_info}

请生成一篇专业的足球比赛预测推文，包含：
1. 吸引人的标题
2. 比赛基本信息
3. 双方实力分析
4. 预测结果（谁获胜）
5. 预测比分
6. 预测总进球数
7. 详细理由和分析

要求：
- 语言专业且生动有趣
- 包含具体的数据支撑
- 字数控制在800-1200字
- 适合微信公众号发布
"""

        messages = [{"role": "user", "content": prompt}]
        result = self.ai_client.chat_completion(messages)

        if result:
            return result['content']
        else:
            return f"AI分析生成失败，请手动编写{home_team} vs {away_team}的比赛预测。"

    def process_all_matches(self):
        """处理所有比赛"""
        print("=== 足球比赛分析工具启动 ===\n")

        # 首先进行文件清理
        self.clean_old_files()

        # 步骤1：获取所有比赛数据
        matches = self.get_match_data()
        if not matches:
            print("没有找到任何比赛，程序结束。")
            return

        print(f"\n=== 步骤2：搜索文章 ===")
        print(f"共有 {len(matches)} 场比赛需要搜索文章")

        # 步骤2：设置浏览器（如果需要提取内容）
        browser_available = self.setup_chrome_driver(enable_manual_verification=True)
        if browser_available:
            print("[成功] 浏览器驱动设置成功，将提取文章内容（支持手动验证）")
            print("[提示] 提示：如遇验证码，程序会自动暂停等待您手动完成验证")
        else:
            print("[失败] 浏览器驱动设置失败，仅保存文章链接")
            print("[警告]  建议检查Chrome浏览器是否正确安装")

        # 步骤3：搜索每场比赛的文章
        processed_count = 0
        skipped_count = 0

        for i, match in enumerate(matches, 1):
            try:
                print(f"\n--- 处理第 {i}/{len(matches)} 场比赛 ---")

                # 重置当前比赛的cookies状态
                self.current_match_cookies_acquired = False

                match_data = match['data']
                keyword = self.generate_search_keyword(match_data)

                if not keyword:
                    print("  无法生成搜索关键词，跳过该比赛")
                    continue

                print(f"  比赛关键词: {keyword}")

                # 检查是否在24小时内已经处理过
                if self.check_match_already_processed(keyword):
                    skipped_count += 1
                    continue

                # 搜索文章
                articles = self.search_articles_for_match(keyword)

                # 保存文章
                if articles:
                    self.save_match_articles(match_data, articles, keyword)
                    processed_count += 1
                    print(f"  [成功] 成功处理比赛 {keyword}")
                else:
                    print(f"  没有找到文章")

                # 添加延迟避免过于频繁的请求（优化：从3秒减少到1秒）
                time.sleep(1)

            except Exception as e:
                print(f"  处理比赛时出现错误: {e}")
                continue

        print(f"\n=== 处理完成 ===")
        print(f"[统计] 处理统计:")
        print(f"  [成功] 新处理比赛: {processed_count} 场")
        print(f"  [跳过]  跳过比赛: {skipped_count} 场（24小时内已处理）")
        print(f"  [保存] 所有文章已保存到: {self.article_dir}")

        # 关闭浏览器
        if self.driver:
            self.driver.quit()
            print("浏览器驱动已关闭")

    def process_matches_with_ai_analysis(self):
        """完整AI分析流程：获取比赛数据->搜索文章->AI分析->一致性判断->生成推文"""
        print("=== 足球比赛AI智能分析工具启动 ===\n")

        # 首先进行文件清理
        self.clean_old_files()

        # 步骤1：获取所有比赛数据
        print("=== 步骤1：获取比赛数据 ===")
        matches = self.get_match_data()
        if not matches:
            print("没有找到任何比赛，程序结束。")
            return

        print(f"\n=== 步骤2：搜索文章 ===")
        print(f"共有 {len(matches)} 场比赛需要搜索文章")

        # 步骤2：设置浏览器
        browser_available = self.setup_chrome_driver(enable_manual_verification=True)
        if browser_available:
            print("[成功] 浏览器驱动设置成功，将提取文章内容")
        else:
            print("[失败] 浏览器驱动设置失败，程序结束")
            return

        # 准备存储所有分析结果
        all_analyses = []
        analysis_file = self.analysis_dir / f"ai_analysis_results_{int(time.time())}.json"

        # 步骤3：搜索每场比赛的文章并进行AI分析
        for i, match in enumerate(matches, 1):
            try:
                print(f"\n--- 处理第 {i}/{len(matches)} 场比赛 ---")

                # 重置当前比赛的cookies状态
                self.current_match_cookies_acquired = False

                match_data = match['data']
                keyword = self.generate_search_keyword(match_data)

                if not keyword:
                    print("  无法生成搜索关键词，跳过该比赛")
                    continue

                print(f"  比赛关键词: {keyword}")

                # 检查是否在24小时内已经处理过
                if self.check_match_already_processed(keyword):
                    print(f"  [跳过]  跳过比赛 {keyword}：24小时内已处理")
                    continue

                # 搜索文章
                articles = self.search_articles_for_match(keyword)

                if not articles:
                    print(f"  没有找到文章")
                    continue

                # 保存文章并提取内容
                self.save_match_articles(match_data, articles, keyword, extract_content=True)

                # 步骤4：AI分析文章内容（使用30线程并发）
                match_analyses = self.analyze_articles_multithreaded(articles, max_workers=30)

                # 保存该比赛的分析结果
                if match_analyses:
                    match_analysis_data = {
                        'match_keyword': keyword,
                        'match_data': match_data,
                        'articles_count': len(articles),
                        'analyses': match_analyses,
                        'analysis_time': datetime.now().isoformat()
                    }
                    all_analyses.append(match_analysis_data)

                    # 实时保存分析结果到JSON文件
                    with open(analysis_file, 'w', encoding='utf-8') as f:
                        json.dump(all_analyses, f, ensure_ascii=False, indent=2, cls=DateTimeEncoder)

                    print(f"  [成功] 成功分析比赛 {keyword}，共 {len(match_analyses)} 篇有效分析")
                else:
                    print(f"  [失败] 该比赛无有效分析结果")

            except Exception as e:
                print(f"  处理比赛时出现错误: {e}")
                continue

        # 关闭浏览器
        if self.driver:
            self.driver.quit()
            print("浏览器驱动已关闭")

        if not all_analyses:
            print("\n[失败] 没有任何有效的分析结果，程序结束")
            return

        # 步骤5：一致性分析和综合判断
        print(f"\n=== 步骤5：一致性分析 ===")
        all_individual_analyses = []
        for match_analysis in all_analyses:
            all_individual_analyses.extend(match_analysis['analyses'])

        consistency_results = self.check_match_consistency(all_individual_analyses)

        # 识别需要重新分析的低一致性比赛（<70%）
        low_consistency_matches = []
        for match_key, consistency in consistency_results.items():
            if consistency['consistency_percentage'] < 70.0:
                low_consistency_matches.append({
                    'match_key': match_key,
                    'consistency': consistency
                })
                print(f"  [警告]  低一致性比赛: {match_key} ({consistency['consistency_percentage']:.1f}%)")

        # 步骤6：对低一致性比赛进行深度分析
        if low_consistency_matches:
            print(f"\n=== 步骤6：深度分析 ({len(low_consistency_matches)} 场争议比赛) ===")
            for low_match in low_consistency_matches:
                match_key = low_match['match_key']
                consistency = low_match['consistency']

                # 找到对应的比赛数据
                match_data = None
                for match_analysis in all_analyses:
                    if match_key in match_analysis['match_keyword']:
                        match_data = match_analysis['match_data']
                        break

                if match_data:
                    print(f"  [分析] 深度分析: {match_key}")
                    # 这里可以添加更深入的分析逻辑
                    # 暂时跳过，使用现有的一致性分析结果
                else:
                    print(f"  [失败] 未找到 {match_key} 的比赛数据")

        # 步骤7：生成微信推文
        print(f"\n=== 步骤7：生成微信推文 ===")

        # 为每场比赛生成推文
        for match_analysis in all_analyses:
            keyword = match_analysis['match_keyword']
            match_data = match_analysis['match_data']
            analyses = match_analysis['analyses']

            if analyses:
                print(f"  [推文] 生成 {keyword} 的推文...")

                # 获取该比赛的一致性信息
                match_consistency = None
                for match_key, consistency in consistency_results.items():
                    if match_key in keyword or keyword in match_key:
                        match_consistency = consistency
                        break

                wechat_post = self.generate_wechat_post(match_data, match_consistency or {}, analyses)

                if wechat_post:
                    # 保存推文到文件
                    wechat_file = self.analysis_dir / f"wechat_post_{keyword}_{int(time.time())}.txt"
                    with open(wechat_file, 'w', encoding='utf-8') as f:
                        f.write(wechat_post)
                    print(f"    [成功] 推文已保存: {wechat_file.name}")
                else:
                    print(f"    [失败] 推文生成失败")

        # 输出最终统计
        print(f"\n=== 分析完成 ===")
        print(f"[统计] 最终统计:")
        print(f"  [比赛] 分析比赛: {len(all_analyses)} 场")
        print(f"  [文章] 总文章数: {sum(len(m['analyses']) for m in all_analyses)} 篇")
        print(f"  [警告]  争议比赛: {len(low_consistency_matches)} 场")
        print(f"  [推文] 生成推文: {len(all_analyses)} 篇")
        print(f"  [分析结果] 分析结果: {analysis_file}")

    def run_interactive(self):
        """交互模式运行"""
        print("=== 足球比赛分析器 ===\n")
        print("请选择功能:")
        print("1. 完整流程：获取比赛数据 + 搜索文章 + 提取内容")
        print("2. 仅获取比赛数据")
        print("3. 搜索文章但不提取内容（快速模式）")
        print("4. 手动搜索指定比赛文章")
        print("5. 测试验证码识别功能（新增）")
        print("6. AI智能分析：完整AI分析流程（推荐）")
        print("7. 完整验证码识别演示（包含API调用）")

        choice = input("\n请选择功能 (1-7): ").strip()

        if choice == "1":
            self.process_all_matches()
        elif choice == "2":
            self.get_match_data()
        elif choice == "3":
            self.process_all_matches_info_only()
        elif choice == "6":
            self.process_matches_with_ai_analysis()
        elif choice == "4":
            keyword = input("请输入搜索关键词 (例如: 曼城vs阿森纳): ").strip()
            if keyword:
                articles = self.search_articles_for_match(keyword)
                print(f"\n找到 {len(articles)} 篇相关文章")
                for i, article in enumerate(articles, 1):
                    print(f"{i}. {article.get('title', '无标题')}")
                    print(f"   链接: {article.get('url', '无链接')}")
                    print(f"   时间: {article.get('publish_time', '未知')}")
                    print()
        elif choice == "5":
            if self.setup_chrome_driver():
                print("    [测试] 测试验证码识别功能...")
                
                # 测试验证码元素查找
                captcha_element = self.find_captcha_element()
                if captcha_element:
                    src = captcha_element.get_attribute('src')
                    class_name = captcha_element.get_attribute('class') or ''
                    element_id = captcha_element.get_attribute('id') or ''
                    width = captcha_element.get_attribute('width')
                    height = captcha_element.get_attribute('height')
                    
                    print(f"    [验证码元素信息]")
                    print(f"      - src类型: {'base64' if src and src.startswith('data:image/') else 'URL'}")
                    print(f"      - class: {class_name}")
                    print(f"      - id: {element_id}")
                    print(f"      - 尺寸: {width}x{height}")
                    print(f"      - src预览: {src[:100] if src else 'None'}...")
                    
                    # 测试base64提取
                    base64_data = self.capture_captcha_image()
                    if base64_data:
                        print(f"    [成功] base64数据长度: {len(base64_data)} 字符")
                        print(f"    [预览] base64前50字符: {base64_data[:50]}...")
                        
                        # 测试verify-msg内容获取
                        verify_msg = self.get_verify_msg_content()
                        print(f"    [verify-msg] 提取的【】内容: '{verify_msg}'")
                        
                        # 如果找到了verify-msg，演示完整的API调用流程
                        if verify_msg and base64_data:
                            print("    [完整测试] 开始测试验证码识别API...")
                            coordinates, api_code = self.call_captcha_api(base64_data, verify_msg)
                            if coordinates:
                                print(f"    [API成功] 获得点击坐标: {coordinates}")
                                print("    [提示] 验证码识别功能完全正常！")
                            elif api_code == 10007:
                                print("    [API提示] 图片识别失败，需要刷新页面重新获取")
                            else:
                                print("    [API失败] 验证码识别API调用失败")
                        elif verify_msg:
                            print("    [部分成功] 找到verify-msg但未获取到base64")
                        else:
                            print("    [警告] 未找到verify-msg元素，但base64提取正常")
                    else:
                        print("    [失败] base64提取失败")
                else:
                    print("    [失败] 未找到验证码元素")
                    
                    # 显示页面上所有图片元素用于调试
                    all_imgs = self.driver.find_elements(By.TAG_NAME, "img")
                    print(f"    [调试] 页面共有 {len(all_imgs)} 个图片元素:")
                    for i, img in enumerate(all_imgs[:5]):  # 只显示前5个
                        src = img.get_attribute('src')
                        class_name = img.get_attribute('class') or ''
                        print(f"      {i+1}. class='{class_name}', src='{src[:50] if src else 'None'}...'")

                self.driver.quit()
            else:
                print("浏览器初始化失败！")
        elif choice == "7":
            print("=== 完整验证码识别演示 ===")
            print("此演示将：")
            print("1. 打开浏览器并访问测试页面")
            print("2. 查找验证码图片和提示文本")
            print("3. 提取base64和【】内容")
            print("4. 调用API获取点击坐标")
            print("5. 模拟点击操作")
            print("6. 检查验证结果")
            
            # 让用户输入测试页面URL或使用默认的
            test_url = input("\n请输入包含验证码的测试页面URL（回车使用默认）: ").strip()
            if not test_url:
                test_url = "https://www.sogou.com"  # 默认测试页面
                
            if self.setup_chrome_driver(enable_manual_verification=True):
                try:
                    print(f"\n[步骤1] 访问测试页面: {test_url}")
                    self.driver.get(test_url)
                    time.sleep(3)
                    
                    print(f"\n[步骤2] 查找验证码元素...")
                    captcha_element = self.find_captcha_element()
                    
                    if captcha_element:
                        print(f"[成功] 找到验证码元素")
                        
                        print(f"\n[步骤3] 提取base64数据...")
                        base64_data = self.capture_captcha_image()
                        
                        print(f"\n[步骤4] 提取【】内容...")
                        verify_msg = self.get_verify_msg_content()
                        
                        if base64_data and verify_msg:
                            print(f"\n[步骤5] 调用验证码识别API...")
                            print(f"  - base64长度: {len(base64_data)} 字符")
                            print(f"  - 提示内容: '{verify_msg}'")
                            
                            coordinates, api_code = self.call_captcha_api(base64_data, verify_msg)
                            
                            if coordinates:
                                print(f"\n[步骤6] 执行点击操作...")
                                print(f"  - 获得坐标: {coordinates}")
                                
                                # 询问是否真的执行点击
                                confirm = input("是否执行实际点击操作？(y/n): ").strip().lower()
                                if confirm == 'y':
                                    success = self.click_captcha_coordinates(coordinates)
                                    if success:
                                        print(f"\n[步骤7] 检查验证结果...")
                                        time.sleep(2)
                                        if self.check_verification_success():
                                            print(f"[步骤8] 点击提交按钮...")
                                            self.click_submit_button()
                                            print(f"\n🎉 完整验证码识别演示成功！")
                                        else:
                                            print(f"\n⚠️ 验证未通过，可能需要重试")
                                    else:
                                        print(f"\n❌ 点击操作失败")
                                else:
                                    print(f"\n✅ 演示完成（未执行实际点击）")
                            elif api_code == 10007:
                                print(f"\n⚠️ 图片识别失败，需要刷新页面重新获取验证码")
                            else:
                                print(f"\n❌ API调用失败")
                        else:
                            print(f"\n❌ 缺少必要数据:")
                            print(f"  - base64: {'✓' if base64_data else '✗'}")
                            print(f"  - verify-msg: {'✓' if verify_msg else '✗'}")
                    else:
                        print(f"\n❌ 当前页面未找到验证码元素")
                        print(f"提示：请访问包含验证码的页面进行测试")
                        
                except Exception as e:
                    print(f"\n❌ 演示过程出错: {e}")
                finally:
                    input("\n按回车键关闭浏览器...")
                    self.driver.quit()
            else:
                print("浏览器初始化失败！")
        else:
            print("无效选择")

    def process_all_matches_info_only(self):
        """处理所有比赛但仅获取文章信息，不提取内容"""
        print("=== 足球比赛分析工具启动（仅文章信息模式）===\n")

        # 首先进行文件清理
        self.clean_old_files()

        # 步骤1：获取所有比赛数据
        matches = self.get_match_data()
        if not matches:
            print("没有找到任何比赛，程序结束。")
            return

        print(f"\n=== 步骤2：搜索文章（仅信息模式）===")
        print(f"共有 {len(matches)} 场比赛需要搜索文章")
        print("此模式仅保存文章信息，不提取完整内容（避免反爬虫问题）")

        # 步骤2：搜索每场比赛的文章
        processed_count = 0
        skipped_count = 0

        for i, match in enumerate(matches, 1):
            try:
                print(f"\n--- 处理第 {i}/{len(matches)} 场比赛 ---")

                # 重置当前比赛的cookies状态
                self.current_match_cookies_acquired = False

                match_data = match['data']
                keyword = self.generate_search_keyword(match_data)

                if not keyword:
                    print("  无法生成搜索关键词，跳过该比赛")
                    continue

                print(f"  比赛关键词: {keyword}")

                # 检查是否在24小时内已经处理过
                if self.check_match_already_processed(keyword):
                    skipped_count += 1
                    continue

                # 搜索文章
                articles = self.search_articles_for_match(keyword)

                # 保存文章信息（不提取内容）
                if articles:
                    self.save_match_articles(match_data, articles, keyword, extract_content=False)
                    processed_count += 1
                    print(f"  [成功] 成功处理比赛 {keyword}")
                else:
                    print(f"  没有找到文章")

                # 添加延迟避免过于频繁的请求
                time.sleep(2)

            except Exception as e:
                print(f"  处理比赛时出现错误: {e}")
                continue

        print(f"\n=== 处理完成 ===")
        print(f"[统计] 处理统计:")
        print(f"  [成功] 新处理比赛: {processed_count} 场")
        print(f"  [跳过]  跳过比赛: {skipped_count} 场（24小时内已处理）")
        print(f"  [保存] 所有文章信息已保存到: {self.article_dir}")
        print("\n提示：文章链接、标题、摘要、发布时间都已完整保存")
        print("   如需获取完整内容，可手动访问保存的文章链接")


def main():
    """主函数"""
    import argparse
    parser = argparse.ArgumentParser(description="足球比赛分析工具")
    parser.add_argument("--searcher", type=str, default="sogou", help="选择搜索引擎 (sogou, sportsdata)")
    parser.add_argument("--auto", action="store_true", help="完整自动流程（包含内容提取）")
    parser.add_argument("--info-only", action="store_true", help="仅获取文章信息（推荐，避免反爬虫）")
    parser.add_argument("--ai-analysis", action="store_true", help="AI智能分析流程")

    args = parser.parse_args()

    analyzer = FootballAnalyzer(searcher_name=args.searcher)

    if args.auto:
        analyzer.process_all_matches()
    elif args.info_only:
        analyzer.process_all_matches_info_only()
    elif args.ai_analysis:
        analyzer.process_matches_with_ai_analysis()
    else:
        analyzer.run_interactive()


if __name__ == "__main__":
    main()