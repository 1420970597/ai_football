#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
足球文章高性能多线程智能分析器
支持50线程并发、自动重试、错误恢复
"""

import os
import json
import time
import requests
from datetime import datetime
from typing import List, Dict, Optional
from pathlib import Path
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue
import logging

# 尝试导入配置文件
try:
    from config import API_CONFIG, ANALYSIS_CONFIG
except ImportError:
    print("警告：未找到config.py文件，将使用默认配置")
    API_CONFIG = {
        "api_token": "sk-",
        "model": "moonshotai/Kimi-K2-Instruct-0905",
        "base_url": "https://api.siliconflow.cn/v1/chat/completions",
        "max_tokens": 200000,
        "temperature": 0.7
    }
    ANALYSIS_CONFIG = {
        "max_content_length": 300000,
        "request_delay": 0.1,
        "max_retries": 10,
        "timeout": 120,
        "max_workers": 50,
        "batch_size": 10
    }


class HighPerformanceAPIClient:
    """高性能API客户端（支持多线程和重试机制）"""

    def __init__(self, api_token: str = None):
        self.api_token = api_token or API_CONFIG["api_token"]
        self.base_url = API_CONFIG["base_url"]
        self.model = API_CONFIG["model"]
        self.max_tokens = API_CONFIG["max_tokens"]
        self.temperature = API_CONFIG["temperature"]

        if not self.api_token:
            raise ValueError("API Token未设置，请在config.py中配置或作为参数传入")

        self.headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }

        # 线程锁和统计
        self.lock = threading.Lock()
        self.request_count = 0
        self.success_count = 0
        self.error_count = 0

        # 设置日志
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        self.logger = logging.getLogger(__name__)

    def chat_completion(self, messages: List[Dict],
                       max_retries: int = None,
                       timeout: int = None,
                       thread_id: str = None) -> Optional[Dict]:
        """调用大模型进行对话（支持重试和错误处理）"""
        max_retries = max_retries or ANALYSIS_CONFIG["max_retries"]
        timeout = timeout or ANALYSIS_CONFIG["timeout"]
        thread_id = thread_id or f"thread-{threading.current_thread().ident}"

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens
        }

        with self.lock:
            self.request_count += 1

        for attempt in range(max_retries):
            try:
                # 添加随机延迟避免请求冲突
                if attempt > 0:
                    delay = ANALYSIS_CONFIG["request_delay"] * (2 ** attempt) + (threading.current_thread().ident % 100) / 1000
                    time.sleep(delay)

                self.logger.debug(f"[{thread_id}] 发送API请求 (尝试 {attempt + 1}/{max_retries})")

                response = requests.post(
                    self.base_url,
                    json=payload,
                    headers=self.headers,
                    timeout=timeout
                )

                # 检查HTTP状态码
                if response.status_code == 200:
                    result = response.json()
                    if 'choices' in result and len(result['choices']) > 0:
                        with self.lock:
                            self.success_count += 1
                        return {
                            'content': result['choices'][0]['message']['content'],
                            'usage': result.get('usage', {}),
                            'model': result.get('model', self.model),
                            'request_id': result.get('id', ''),
                            'thread_id': thread_id
                        }
                    else:
                        self.logger.warning(f"[{thread_id}] API响应格式异常: {result}")
                elif response.status_code == 429:
                    # 频率限制，增加延迟
                    self.logger.warning(f"[{thread_id}] 遇到频率限制，等待重试...")
                    time.sleep(5 + attempt * 2)
                    continue
                elif response.status_code >= 500:
                    # 服务器错误，重试
                    self.logger.warning(f"[{thread_id}] 服务器错误 {response.status_code}，重试中...")
                    continue
                else:
                    # 其他错误
                    self.logger.error(f"[{thread_id}] API调用失败，状态码: {response.status_code}, 响应: {response.text}")

            except requests.exceptions.Timeout:
                self.logger.warning(f"[{thread_id}] 请求超时 (尝试 {attempt + 1}/{max_retries})")
            except requests.exceptions.ConnectionError:
                self.logger.warning(f"[{thread_id}] 连接错误 (尝试 {attempt + 1}/{max_retries})")
            except requests.exceptions.RequestException as e:
                self.logger.warning(f"[{thread_id}] 请求异常: {e} (尝试 {attempt + 1}/{max_retries})")
            except json.JSONDecodeError:
                self.logger.warning(f"[{thread_id}] JSON解析失败 (尝试 {attempt + 1}/{max_retries})")
            except Exception as e:
                self.logger.error(f"[{thread_id}] 未知错误: {e}")

        with self.lock:
            self.error_count += 1
        self.logger.error(f"[{thread_id}] API调用最终失败，已重试 {max_retries} 次")
        return None

    def get_stats(self) -> Dict:
        """获取API调用统计"""
        with self.lock:
            return {
                'total_requests': self.request_count,
                'successful_requests': self.success_count,
                'failed_requests': self.error_count,
                'success_rate': self.success_count / max(self.request_count, 1) * 100
            }


class MultiThreadArticleAnalyzer:
    """多线程文章分析器"""

    def __init__(self, api_token: str = None):
        self.client = HighPerformanceAPIClient(api_token)
        self.output_dir = Path("output")
        self.articles_dir = self.output_dir / "articles"
        self.analysis_dir = self.output_dir / "analysis"

        # 创建分析结果目录
        self.analysis_dir.mkdir(exist_ok=True)

        # 线程相关
        self.max_workers = ANALYSIS_CONFIG["max_workers"]
        self.batch_size = ANALYSIS_CONFIG["batch_size"]

        # 统计信息
        self.stats = {
            'total_articles': 0,
            'successful_analyses': 0,
            'failed_analyses': 0,
            'total_tokens_used': 0,
            'start_time': None,
            'end_time': None
        }

        # 线程锁
        self.stats_lock = threading.Lock()
        self.progress_lock = threading.Lock()

        # 设置日志
        self.logger = logging.getLogger(__name__)

    def update_stats(self, **kwargs):
        """线程安全的统计更新"""
        with self.stats_lock:
            for key, value in kwargs.items():
                if key in self.stats:
                    if isinstance(value, (int, float)):
                        self.stats[key] += value
                    else:
                        self.stats[key] = value

    def load_progress(self, progress_file: Path) -> Dict:
        """加载分析进度"""
        if progress_file.exists():
            try:
                with open(progress_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return {'completed_files': [], 'analyses': []}
        return {'completed_files': [], 'analyses': []}

    def save_progress(self, progress_file: Path, progress: Dict):
        """保存分析进度（线程安全）"""
        with self.progress_lock:
            try:
                with open(progress_file, 'w', encoding='utf-8') as f:
                    json.dump(progress, f, ensure_ascii=False, indent=2)
            except Exception as e:
                self.logger.error(f"保存进度失败: {e}")

    def find_all_articles(self, match_filter: str = None) -> List[Dict]:
        """找到所有文章JSON文件"""
        articles = []

        if not self.articles_dir.exists():
            self.logger.error("文章目录不存在")
            return articles

        for match_folder in self.articles_dir.iterdir():
            if not match_folder.is_dir() or not match_folder.name.startswith("match_"):
                continue

            # 如果有过滤条件，检查是否匹配
            if match_filter and match_filter not in match_folder.name:
                continue

            # 解析比赛信息
            try:
                match_info_file = match_folder / "match_info.json"
                if match_info_file.exists():
                    with open(match_info_file, 'r', encoding='utf-8') as f:
                        match_info = json.load(f)
                else:
                    match_info = {}
            except Exception as e:
                self.logger.warning(f"读取比赛信息失败 {match_folder.name}: {e}")
                match_info = {}

            # 查找所有文章文件
            for article_file in match_folder.glob("article_*.json"):
                try:
                    with open(article_file, 'r', encoding='utf-8') as f:
                        article_data = json.load(f)

                    # 检查文章是否有有效内容
                    content_data = article_data.get('content_data', {})
                    if not content_data.get('success') or not content_data.get('text'):
                        continue

                    articles.append({
                        'match_folder': match_folder.name,
                        'article_file': article_file.name,
                        'file_path': str(article_file),
                        'relative_path': f"{match_folder.name}/{article_file.name}",
                        'match_info': match_info,
                        'article_data': article_data
                    })
                except Exception as e:
                    self.logger.warning(f"读取文章文件失败 {article_file}: {e}")

        return articles

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
            "analysis": "对该比赛的分析结果（包括比分预测、优势分析、关键因素等）"
        }}
    ],
    "summary": "文章整体分析总结",
    "confidence": "分析置信度（高/中/低）"
}}

请确保回答是有效的JSON格式，用中文回答。如果文章中没有明确的比赛信息，matches数组可以为空。
"""

    def analyze_single_article(self, article: Dict, thread_id: str = None) -> Optional[Dict]:
        """分析单篇文章（线程安全）"""
        article_info = article['article_data'].get('article_info', {})
        content_data = article['article_data'].get('content_data', {})

        title = article_info.get('title', '无标题')
        thread_id = thread_id or f"thread-{threading.current_thread().ident}"

        # 创建分析提示词
        prompt = self.create_analysis_prompt(article_info, content_data)

        messages = [
            {
                "role": "user",
                "content": prompt
            }
        ]

        self.logger.info(f"[{thread_id}] 正在分析文章: {title[:50]}...")

        result = self.client.chat_completion(messages, thread_id=thread_id)

        if result:
            # 尝试解析JSON响应
            try:
                analysis_json = json.loads(result['content'])

                analysis = {
                    'article_info': {
                        'title': title,
                        'summary': article_info.get('summary', ''),
                        'url': article_info.get('url', ''),
                        'publish_time': article_info.get('publish_time', ''),
                        'file_path': article['file_path'],
                        'relative_path': article['relative_path']
                    },
                    'analysis_result': analysis_json,
                    'raw_response': result['content'],
                    'usage': result.get('usage', {}),
                    'analysis_time': datetime.now().isoformat(),
                    'thread_id': thread_id
                }

                # 更新统计
                tokens_used = result.get('usage', {}).get('total_tokens', 0)
                self.update_stats(successful_analyses=1, total_tokens_used=tokens_used)

                self.logger.info(f"[{thread_id}] 分析成功: {title[:30]}...")
                return analysis

            except json.JSONDecodeError:
                self.logger.warning(f"[{thread_id}] JSON解析失败，保存原始响应")
                analysis = {
                    'article_info': {
                        'title': title,
                        'summary': article_info.get('summary', ''),
                        'url': article_info.get('url', ''),
                        'publish_time': article_info.get('publish_time', ''),
                        'file_path': article['file_path'],
                        'relative_path': article['relative_path']
                    },
                    'analysis_result': {'raw_text': result['content']},
                    'raw_response': result['content'],
                    'usage': result.get('usage', {}),
                    'analysis_time': datetime.now().isoformat(),
                    'thread_id': thread_id
                }

                tokens_used = result.get('usage', {}).get('total_tokens', 0)
                self.update_stats(successful_analyses=1, total_tokens_used=tokens_used)
                return analysis
        else:
            self.logger.error(f"[{thread_id}] 分析失败: {title[:30]}...")
            self.update_stats(failed_analyses=1)
            return None

    def batch_analyze_articles(self, articles: List[Dict], progress: Dict, progress_file: Path):
        """批量分析文章（多线程）"""
        self.logger.info(f"开始多线程分析，使用 {self.max_workers} 个线程")

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # 提交所有任务
            future_to_article = {}
            for i, article in enumerate(articles):
                thread_id = f"worker-{i % self.max_workers}"
                future = executor.submit(self.analyze_single_article, article, thread_id)
                future_to_article[future] = article

            # 处理完成的任务
            completed = 0
            for future in as_completed(future_to_article):
                article = future_to_article[future]
                completed += 1

                try:
                    analysis = future.result()
                    if analysis:
                        with self.progress_lock:
                            progress['analyses'].append(analysis)
                            progress['completed_files'].append(article['relative_path'])

                        self.logger.info(f"进度: {completed}/{len(articles)} - 成功分析文章")
                    else:
                        self.logger.warning(f"进度: {completed}/{len(articles)} - 分析失败")

                    # 定期保存进度
                    if completed % 10 == 0:
                        self.save_progress(progress_file, progress)

                except Exception as e:
                    self.logger.error(f"处理分析结果时出错: {e}")
                    self.update_stats(failed_analyses=1)

                # 显示总体进度
                if completed % 10 == 0 or completed == len(articles):
                    api_stats = self.client.get_stats()
                    self.logger.info(f"总进度: {completed}/{len(articles)}, "
                                   f"API成功率: {api_stats['success_rate']:.1f}%, "
                                   f"已用Token: {self.stats['total_tokens_used']}")

    def summarize_all_analyses(self, analyses: List[Dict]) -> Optional[Dict]:
        """汇总所有分析结果"""
        if not analyses:
            return None

        # 提取所有比赛和分析
        all_matches = []
        all_summaries = []

        for analysis in analyses:
            result = analysis.get('analysis_result', {})
            if isinstance(result, dict) and 'matches' in result:
                all_matches.extend(result['matches'])
                if 'summary' in result:
                    all_summaries.append(result['summary'])

        # 构建汇总提示词
        summary_content = f"分析了 {len(analyses)} 篇足球文章，提取的比赛信息如下：\n\n"

        # 添加比赛信息
        for i, match in enumerate(all_matches, 1):
            if isinstance(match, dict):
                home = match.get('home_team', '未知')
                away = match.get('away_team', '未知')
                analysis = match.get('analysis', '无分析')
                summary_content += f"比赛 {i}：{home} vs {away}\n分析：{analysis}\n\n"

        # 添加文章总结
        if all_summaries:
            summary_content += "文章总结：\n"
            for i, summary in enumerate(all_summaries, 1):
                summary_content += f"{i}. {summary}\n"

        final_prompt = f"""
请对以下足球文章分析结果进行综合汇总：

{summary_content}

请按照以下JSON格式提供最终分析：
{{
    "total_matches": 比赛总数,
    "unique_matches": [
        {{
            "home_team": "主队",
            "away_team": "客队",
            "combined_analysis": "综合分析结论",
            "confidence_level": "置信度"
        }}
    ],
    "overall_insights": "整体洞察和趋势分析",
    "betting_suggestions": "投注建议（如果有的话）",
    "data_quality": "数据质量评估"
}}

请确保回答是有效的JSON格式，用中文回答。
"""

        messages = [
            {
                "role": "user",
                "content": final_prompt
            }
        ]

        self.logger.info("开始最终汇总分析...")

        result = self.client.chat_completion(messages, thread_id="summary-thread")
        if result:
            try:
                return {
                    'summary_json': json.loads(result['content']),
                    'raw_response': result['content'],
                    'usage': result.get('usage', {}),
                    'summary_time': datetime.now().isoformat()
                }
            except json.JSONDecodeError:
                return {
                    'summary_json': {'raw_text': result['content']},
                    'raw_response': result['content'],
                    'usage': result.get('usage', {}),
                    'summary_time': datetime.now().isoformat()
                }
        return None

    def save_analysis_results(self, analyses: List[Dict], final_summary: Dict):
        """保存分析结果"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # 获取API统计
        api_stats = self.client.get_stats()

        # 保存详细分析结果
        detailed_file = self.analysis_dir / f"detailed_analysis_{timestamp}.json"
        with open(detailed_file, 'w', encoding='utf-8') as f:
            json.dump({
                'metadata': {
                    'analysis_time': datetime.now().isoformat(),
                    'total_articles': len(analyses),
                    'model_used': self.client.model,
                    'api_endpoint': self.client.base_url,
                    'max_workers': self.max_workers,
                    'stats': self.stats,
                    'api_stats': api_stats
                },
                'detailed_analyses': analyses,
                'final_summary': final_summary
            }, f, ensure_ascii=False, indent=2)

        # 保存可读的汇总报告
        summary_file = self.analysis_dir / f"summary_report_{timestamp}.txt"
        with open(summary_file, 'w', encoding='utf-8') as f:
            f.write(f"足球文章高性能智能分析汇总报告\n")
            f.write("=" * 60 + "\n")
            f.write(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"分析文章数量：{len(analyses)} 篇\n")
            f.write(f"使用模型：{self.client.model}\n")
            f.write(f"并发线程：{self.max_workers} 个\n")
            f.write(f"API成功率：{api_stats['success_rate']:.1f}%\n")
            f.write(f"总Token使用：{self.stats['total_tokens_used']}\n")
            f.write("=" * 60 + "\n\n")

            # 写入最终汇总
            if 'summary_json' in final_summary:
                summary_data = final_summary['summary_json']
                if isinstance(summary_data, dict):
                    f.write("📊 综合分析结果：\n\n")

                    if 'unique_matches' in summary_data:
                        f.write("🏆 比赛分析：\n")
                        for i, match in enumerate(summary_data['unique_matches'], 1):
                            if isinstance(match, dict):
                                f.write(f"{i}. {match.get('home_team', '未知')} vs {match.get('away_team', '未知')}\n")
                                f.write(f"   分析：{match.get('combined_analysis', '无分析')}\n")
                                f.write(f"   置信度：{match.get('confidence_level', '未知')}\n\n")

                    if 'overall_insights' in summary_data:
                        f.write(f"💡 整体洞察：\n{summary_data['overall_insights']}\n\n")

                    if 'betting_suggestions' in summary_data:
                        f.write(f"💰 投注建议：\n{summary_data['betting_suggestions']}\n\n")
                else:
                    f.write(final_summary.get('raw_response', '分析结果格式异常'))
            else:
                f.write(final_summary.get('raw_response', '无法获取分析结果'))

        self.logger.info(f"分析结果已保存:")
        self.logger.info(f"  详细数据: {detailed_file}")
        self.logger.info(f"  汇总报告: {summary_file}")

        return detailed_file, summary_file

    def run_analysis(self, match_filter: str = None, resume: bool = False):
        """运行完整的分析流程"""
        self.stats['start_time'] = datetime.now().isoformat()

        self.logger.info("=== 足球文章高性能多线程智能分析器启动 ===")
        self.logger.info(f"配置信息: {self.max_workers}线程, 最大重试{ANALYSIS_CONFIG['max_retries']}次")
        self.logger.info(f"API地址: {self.client.base_url}")
        self.logger.info(f"使用模型: {self.client.model}")

        # 设置进度文件
        progress_file = self.analysis_dir / "analysis_progress.json"
        progress = self.load_progress(progress_file) if resume else {'completed_files': [], 'analyses': []}

        # 查找所有文章
        self.logger.info("正在查找文章文件...")
        articles = self.find_all_articles(match_filter)

        if not articles:
            self.logger.error("没有找到任何文章文件")
            return

        # 过滤已完成的文章（如果是断点续传）
        if resume and progress['completed_files']:
            articles = [a for a in articles if a['relative_path'] not in progress['completed_files']]
            self.logger.info(f"断点续传：跳过已完成的 {len(progress['completed_files'])} 篇文章")

        self.stats['total_articles'] = len(articles)
        self.logger.info(f"需要分析 {len(articles)} 篇文章")

        if not articles:
            self.logger.info("所有文章都已分析完成")
            if progress['analyses']:
                self.logger.info("开始最终汇总...")
                final_summary = self.summarize_all_analyses(progress['analyses'])
                if final_summary:
                    self.save_analysis_results(progress['analyses'], final_summary)
            return

        # 批量分析文章（多线程）
        self.logger.info("开始批量分析文章...")
        self.batch_analyze_articles(articles, progress, progress_file)

        self.stats['end_time'] = datetime.now().isoformat()

        # 获取最终统计
        api_stats = self.client.get_stats()

        self.logger.info("文章分析统计:")
        self.logger.info(f"  ✅ 成功分析: {self.stats['successful_analyses']} 篇")
        self.logger.info(f"  ❌ 分析失败: {self.stats['failed_analyses']} 篇")
        self.logger.info(f"  🔤 使用Token: {self.stats['total_tokens_used']}")
        self.logger.info(f"  📊 API成功率: {api_stats['success_rate']:.1f}%")

        if not progress['analyses']:
            self.logger.error("没有成功分析的文章，无法进行汇总")
            return

        # 进行最终汇总
        self.logger.info("正在进行最终汇总分析...")
        final_summary = self.summarize_all_analyses(progress['analyses'])

        if final_summary:
            self.logger.info("汇总分析完成")

            # 更新token统计
            if 'usage' in final_summary:
                self.update_stats(total_tokens_used=final_summary['usage'].get('total_tokens', 0))

            # 保存结果
            detailed_file, summary_file = self.save_analysis_results(progress['analyses'], final_summary)

            # 显示汇总结果
            print("\n" + "=" * 60)
            print("📋 最终汇总分析结果：")
            print("=" * 60)

            if 'summary_json' in final_summary and isinstance(final_summary['summary_json'], dict):
                summary_data = final_summary['summary_json']

                if 'unique_matches' in summary_data:
                    print("🏆 比赛分析：")
                    for i, match in enumerate(summary_data['unique_matches'], 1):
                        if isinstance(match, dict):
                            print(f"{i}. {match.get('home_team', '未知')} vs {match.get('away_team', '未知')}")
                            print(f"   {match.get('combined_analysis', '无分析')}")

                if 'overall_insights' in summary_data:
                    print(f"\n💡 整体洞察：\n{summary_data['overall_insights']}")
            else:
                print(final_summary.get('raw_response', '分析结果格式异常'))

            print("=" * 60)

            # 清除进度文件
            if progress_file.exists():
                progress_file.unlink()
                self.logger.info("已清除进度文件")

        else:
            self.logger.error("最终汇总失败")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='足球文章高性能多线程智能分析器')
    parser.add_argument('--token', type=str, help='API Token')
    parser.add_argument('--filter', type=str, help='比赛文件夹过滤关键词')
    parser.add_argument('--resume', action='store_true', help='断点续传')
    parser.add_argument('--workers', type=int, default=None, help='线程数量')
    parser.add_argument('--verbose', '-v', action='store_true', help='详细日志')

    args = parser.parse_args()

    # 设置日志级别
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
    else:
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    print("=== 足球文章高性能多线程智能分析器 ===")

    # 获取API Token
    api_token = args.token
    if not api_token:
        if API_CONFIG["api_token"] and API_CONFIG["api_token"] != "sk-":
            api_token = API_CONFIG["api_token"]
            print("✅ 使用配置文件中的API Token")
        else:
            api_token = input("请输入API Token: ").strip()

    if not api_token:
        print("❌ API Token不能为空")
        return

    # 设置线程数
    if args.workers:
        ANALYSIS_CONFIG["max_workers"] = args.workers
        print(f"🔧 设置线程数为: {args.workers}")

    try:
        # 创建分析器并运行
        analyzer = MultiThreadArticleAnalyzer(api_token)
        analyzer.run_analysis(match_filter=args.filter, resume=args.resume)

    except KeyboardInterrupt:
        print("\n\n⚠️ 用户中断了程序")
        print("💾 当前进度已保存，可使用 --resume 参数继续")
    except Exception as e:
        print(f"\n❌ 程序运行出错: {e}")
        import traceback
        traceback.print_exc()

    print("\n程序结束")


if __name__ == "__main__":
    main()