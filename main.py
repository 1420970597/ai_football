#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
足球比赛分析工具
自动获取比赛数据，搜索相关文章，并提取内容
"""

import os
import json
import time
import re
import shutil
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from pathlib import Path

class DateTimeEncoder(json.JSONEncoder):
    """自定义JSON编码器，处理datetime对象"""
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)

# 导入自定义模块
from match_generator import get_football_data_single_files
from article_search import ArticleSearcher

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
    """足球比赛分析器"""

    def __init__(self):
        self.output_dir = Path("output")
        self.article_dir = self.output_dir / "articles"
        self.searcher = ArticleSearcher()
        self.driver = None
        self.current_match_cookies_acquired = False  # 当前比赛是否已获取cookies

        # 创建输出目录
        self.article_dir.mkdir(parents=True, exist_ok=True)

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

            # 使用webdriver-manager自动下载和管理ChromeDriver
            try:
                service = Service(ChromeDriverManager().install())
                self.driver = webdriver.Chrome(service=service, options=chrome_options)

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
            print("    ❌ 浏览器驱动未初始化")
            return False

        try:
            print(f"    🍪 为比赛 {match_keyword} 获取新的cookies...")

            # 步骤1：访问sogou.com获取cookies
            self.driver.get("https://www.sogou.com")
            time.sleep(3)  # 等待页面加载

            # 获取当前cookies数量
            current_cookies = self.driver.get_cookies()
            sogou_cookies = [c for c in current_cookies if '.sogou.com' in c.get('domain', '')]

            print(f"    ✅ 成功从sogou.com获取到 {len(sogou_cookies)} 个相关cookies")

            # 标记当前比赛已获取cookies
            self.current_match_cookies_acquired = True

            return True

        except Exception as e:
            print(f"    ❌ 获取cookies失败: {e}")
            self.current_match_cookies_acquired = False
            return False

    def clean_old_files(self):
        """清理72小时前的文件夹和比赛JSON文件"""
        current_time = datetime.now()
        cutoff_time = current_time - timedelta(hours=72)

        print("🧹 开始清理72小时前的旧文件...")
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
            print(f"✅ 清理完成：删除了 {cleaned_folders} 个文章文件夹，{cleaned_json_files} 个比赛JSON文件")
        else:
            print("✅ 无需清理：没有发现72小时前的旧文件")

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
                                print(f"  ⏭️  跳过比赛 {keyword}：24小时内已处理（文件夹：{folder.name}）")
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
            print(f"    🔄 检测到新比赛，正在获取cookies...")
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

            # 更自然的访问行为
            import random

            # 随机延迟模拟真实用户
            delay = random.uniform(3, 7)
            print(f"    等待 {delay:.1f} 秒...")
            time.sleep(delay)

            # 访问目标URL
            self.driver.get(url)

            # 等待页面加载，使用更智能的等待
            try:
                WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                # 额外等待JavaScript执行
                time.sleep(random.uniform(2, 4))
            except:
                print("    页面加载超时，继续尝试...")

            # 检查是否遇到验证码页面
            title = self.driver.title
            page_source = self.driver.page_source

            # 更全面的验证页面检测
            verification_indicators = [
                "搜狗" in title and "验证" in page_source,
                "安全验证" in page_source,
                "请点击" in page_source and "验证码" in page_source,
                "security verification" in page_source.lower(),
                "captcha" in page_source.lower(),
                "robot" in page_source.lower(),
                "滑动验证" in page_source
            ]

            if any(verification_indicators):
                # 如果启用了手动验证模式，等待用户完成验证
                if hasattr(self, 'manual_verification_enabled') and self.manual_verification_enabled:
                    print("    检测到验证页面，等待手动验证...")
                    if self.wait_for_manual_verification():
                        # 验证成功，重新获取页面信息
                        title = self.driver.title
                        page_source = self.driver.page_source
                        print("    验证完成，继续提取内容...")
                    else:
                        return {
                            'success': False,
                            'error': '手动验证超时或失败',
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

        # 只有在启用内容提取且浏览器可用时才尝试提取内容
        if extract_content and self.driver and articles:
            print(f"  开始提取 {len(articles)} 篇文章的详细内容...")
            successful_extractions = 0
            blocked_by_antibot = 0

            for i, article in enumerate(articles, 1):
                print(f"  处理文章 {i}/{len(articles)}")

                url = article.get('url', '')
                if not url:
                    print(f"    文章 {i} 缺少URL，跳过")
                    continue

                # 提取文章内容
                content_data = self.extract_article_content(url, keyword)

                # 优化content_data，移除无用的HTML内容
                optimized_content_data = {
                    'success': content_data.get('success', False),
                    'title': content_data.get('title', ''),
                    'text': content_data.get('text', ''),
                    'url': content_data.get('url', ''),
                    'access_time': content_data.get('access_time', ''),
                    'error': content_data.get('error', '')
                }

                # 保存文章数据（仅保存JSON，不保存HTML）
                article_file = match_folder / f"article_{i:03d}.json"
                combined_data = {
                    'article_info': article,
                    'content_data': optimized_content_data
                }

                try:
                    with open(article_file, 'w', encoding='utf-8', errors='ignore') as f:
                        json.dump(combined_data, f, ensure_ascii=False, indent=2, cls=DateTimeEncoder)
                except Exception as e:
                    print(f"    保存文章 {i} 失败: {e}")

                # 统计结果
                if optimized_content_data.get('success'):
                    successful_extractions += 1
                elif '反爬虫' in optimized_content_data.get('error', ''):
                    blocked_by_antibot += 1

                # 添加延迟避免过于频繁的请求
                time.sleep(3)

            # 输出统计结果
            print(f"  📊 内容提取统计:")
            print(f"    ✅ 成功提取: {successful_extractions} 篇")
            print(f"    🛡️  反爬虫拦截: {blocked_by_antibot} 篇")
            print(f"    ❌ 其他错误: {len(articles) - successful_extractions - blocked_by_antibot} 篇")

            if successful_extractions > 0:
                success_rate = (successful_extractions / len(articles)) * 100
                print(f"    📈 成功率: {success_rate:.1f}%")

            if blocked_by_antibot > 0:
                print(f"  ⚠️  关于反爬虫拦截的说明：")
                print(f"     1. 文章基本信息已完整保存（标题、摘要、发布时间、链接）")
                print(f"     2. 程序已支持手动验证功能，遇到验证码会自动暂停等待")
                print(f"     3. 可直接点击保存的链接手动访问获取完整内容")
                print(f"     4. 建议适当间隔后重试，或在不同时间段运行")

        elif not extract_content:
            print(f"  仅保存文章信息，跳过内容提取")
        else:
            print(f"  浏览器未启动，仅保存文章信息")

        print(f"  比赛 {keyword} 的所有文章已保存到: {match_folder}")

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
            print("✅ 浏览器驱动设置成功，将提取文章内容（支持手动验证）")
            print("💡 提示：如遇验证码，程序会自动暂停等待您手动完成验证")
        else:
            print("❌ 浏览器驱动设置失败，仅保存文章链接")
            print("⚠️  建议检查Chrome浏览器是否正确安装")

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
                    print(f"  ✅ 成功处理比赛 {keyword}")
                else:
                    print(f"  没有找到文章")

                # 添加延迟避免过于频繁的请求
                time.sleep(3)

            except Exception as e:
                print(f"  处理比赛时出现错误: {e}")
                continue

        print(f"\n=== 处理完成 ===")
        print(f"📊 处理统计:")
        print(f"  ✅ 新处理比赛: {processed_count} 场")
        print(f"  ⏭️  跳过比赛: {skipped_count} 场（24小时内已处理）")
        print(f"  📁 所有文章已保存到: {self.article_dir}")

        # 关闭浏览器
        if self.driver:
            self.driver.quit()
            print("浏览器驱动已关闭")

    def run_interactive(self):
        """交互模式运行"""
        print("=== 足球比赛分析器 ===\n")
        print("请选择功能:")
        print("1. 完整流程：获取比赛数据 + 搜索文章 + 提取内容")
        print("2. 仅获取比赛数据")
        print("3. 搜索文章但不提取内容（快速模式）")
        print("4. 手动搜索指定比赛文章")
        print("5. 测试浏览器功能")

        choice = input("\n请选择功能 (1-5): ").strip()

        if choice == "1":
            self.process_all_matches()
        elif choice == "2":
            self.get_match_data()
        elif choice == "3":
            self.process_all_matches_info_only()
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
                print("测试成功！开始cookie获取流程测试...")

                # 步骤1：访问sogou.com获取cookies
                print("步骤1: 访问sogou.com获取初始cookies")
                try:
                    self.driver.get("https://www.sogou.com")
                    time.sleep(3)  # 等待页面加载

                    # 获取当前所有cookies
                    current_cookies = self.driver.get_cookies()
                    print(f"从sogou.com获取到 {len(current_cookies)} 个cookies:")
                    for cookie in current_cookies:
                        print(f"  - {cookie['name']}: {cookie['value'][:50]}...")

                except Exception as e:
                    print(f"访问sogou.com失败: {e}")
                    self.driver.quit()
                    return

                # 步骤2：将获取到的cookies设置到浏览器中（实际上已经自动设置了）
                print("\n步骤2: cookies已自动设置到浏览器中")

                # 步骤3：访问测试文章页面
                print("\n步骤3: 访问测试文章页面")
                test_url = "https://weixin.sogou.com/link?url=dn9a_-gY295K0Rci_xozVXfdMkSQTLW6cwJThYulHEtVjXrGTiVgSyoZqaC7I7jeFU7rKkfacLNI6NFSZQIFIFqXa8Fplpd93EUe7oxH1zLDywLb_nyCVYu2swwBQCF5dXM6HoztHrNefejiomZIVDGfUkNBHFr40d-bq7lato7bdSbGwLKUcu8ThOCFmXHBsuRIpg2OyMmaEX_8uDfRITzYOtEwnCdWvDomAVj6HvlF3y02GNQhUIIUKabx8-fVsqM116sz0Ej1AfaFW2DefQ..&type=2&query=%E4%BC%AF%E6%81%A9%E8%8C%85vs%E7%BA%BD%E5%8D%A1%E6%96%AF&token=CAE5577187AA9A166E685125BCD287366EF2BEBE68CF8AE8"

                result = self.extract_article_content(test_url, "测试比赛")
                print(f"\n步骤4: 测试结果")
                print(f"访问结果: {'成功' if result['success'] else '失败'}")
                if result['success']:
                    print(f"页面标题: {result['title']}")
                    print(f"内容长度: {len(result['text'])} 字符")
                    if len(result['text']) > 100:
                        print(f"内容预览: {result['text'][:200]}...")
                    print("\n✅ Cookie获取流程测试成功！")
                else:
                    print(f"错误信息: {result['error']}")
                    print("\n❌ Cookie获取流程测试失败")

                # 步骤5：显示最终的cookie状态
                print(f"\n步骤5: 最终cookie状态")
                final_cookies = self.driver.get_cookies()
                sogou_cookies = [c for c in final_cookies if '.sogou.com' in c.get('domain', '')]
                print(f"搜狗域名下的cookies: {len(sogou_cookies)} 个")
                for cookie in sogou_cookies:
                    print(f"  - {cookie['name']}: {cookie['value'][:30]}...")

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
                    print(f"  ✅ 成功处理比赛 {keyword}")
                else:
                    print(f"  没有找到文章")

                # 添加延迟避免过于频繁的请求
                time.sleep(2)

            except Exception as e:
                print(f"  处理比赛时出现错误: {e}")
                continue

        print(f"\n=== 处理完成 ===")
        print(f"📊 处理统计:")
        print(f"  ✅ 新处理比赛: {processed_count} 场")
        print(f"  ⏭️  跳过比赛: {skipped_count} 场（24小时内已处理）")
        print(f"  📁 所有文章信息已保存到: {self.article_dir}")
        print("\n提示：文章链接、标题、摘要、发布时间都已完整保存")
        print("   如需获取完整内容，可手动访问保存的文章链接")


def main():
    """主函数"""
    analyzer = FootballAnalyzer()

    # 检查命令行参数自动运行
    import sys
    if len(sys.argv) > 1:
        if sys.argv[1] == "--auto":
            # 自动模式：直接运行完整流程
            analyzer.process_all_matches()
        elif sys.argv[1] == "--info-only":
            # 仅文章信息模式：搜索文章但不提取内容
            analyzer.process_all_matches_info_only()
        elif sys.argv[1] == "--help":
            print("足球比赛分析工具使用说明:")
            print("  python main.py              # 交互模式")
            print("  python main.py --auto       # 完整自动流程（包含内容提取）")
            print("  python main.py --info-only  # 仅获取文章信息（推荐，避免反爬虫）")
            print("  python main.py --help       # 显示此帮助信息")
        else:
            print(f"未知参数: {sys.argv[1]}")
            print("使用 --help 查看可用参数")
    else:
        # 交互模式
        analyzer.run_interactive()


if __name__ == "__main__":
    main()