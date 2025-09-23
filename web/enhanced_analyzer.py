#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
足球文章深度智能分析器（增强版）
包含一致性检查、深度分析和微信推文生成功能
"""

import os
import json
import time
import requests
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from pathlib import Path
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue
import logging
from collections import defaultdict, Counter
import statistics

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


class AdvancedHighPerformanceAPIClient:
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


class AdvancedMultiThreadArticleAnalyzer:
    """高级多线程文章分析器（包含一致性检查和深度分析）"""

    def __init__(self, api_token: str = None):
        self.client = AdvancedHighPerformanceAPIClient(api_token)
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
            'low_consistency_matches': 0,
            'deep_analyses_count': 0,
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

    def load_match_data(self) -> Dict:
        """加载比赛基础数据（足彩统计数据）"""
        match_data = {}

        # 查找比赛JSON文件
        for json_file in self.output_dir.glob("场次*.json"):
            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                basic_info = data.get('基本信息', {})
                home_team = basic_info.get('主队名称', '').strip()
                away_team = basic_info.get('客队名称', '').strip()

                if home_team and away_team:
                    match_key = f"{home_team}vs{away_team}".replace(' ', '').replace('  ', '')
                    match_data[match_key] = data

            except Exception as e:
                self.logger.warning(f"读取比赛数据失败 {json_file}: {e}")

        self.logger.info(f"加载了 {len(match_data)} 场比赛的足彩数据")
        return match_data

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
                'predictions': predictions,
                'result_distribution': dict(result_counts)
            }

            self.logger.info(f"比赛 {match_key}: 一致性 {consistency_percentage:.1f}%, "
                           f"主要预测 {most_common_result}, 预测数量 {total_predictions}")

        return consistency_results

    def create_deep_analysis_prompt(self, match_key: str, match_data: Dict,
                                  predictions: List[Dict], consistency_info: Dict) -> str:
        """为低一致性比赛创建深度分析提示词"""

        # 提取比赛基本信息
        basic_info = match_data.get('基本信息', {})
        odds_info = match_data.get('赔率信息', {})
        injury_info = match_data.get('伤停信息', {})
        future_matches = match_data.get('详细分析数据', {}).get('未来比赛', {})

        # 收集所有文章的预测
        predictions_text = ""
        for i, pred in enumerate(predictions, 1):
            analysis_data = pred['analysis']
            article_title = analysis_data.get('article_info', {}).get('title', f'文章{i}')
            prediction = pred['result_prediction']
            confidence = pred['confidence']
            predictions_text += f"{i}. {article_title}: 预测{prediction}, 置信度{confidence}\n"

        prompt = f"""
请对以下足球比赛进行深度分析，该比赛的媒体预测存在分歧（一致性仅{consistency_info['consistency_percentage']:.1f}%）：

比赛信息：
主队：{basic_info.get('主队名称', '未知')}
客队：{basic_info.get('客队名称', '未知')}
比赛日期：{basic_info.get('比赛日期', '未知')}
联赛：{basic_info.get('联赛名称', '未知')}

赔率信息：
主胜：{odds_info.get('主胜赔率', '未知')}
平局：{odds_info.get('平局赔率', '未知')}
客胜：{odds_info.get('客胜赔率', '未知')}

媒体预测分布：
{predictions_text}

现有预测分歧情况：
{consistency_info.get('result_distribution', {})}

请从以下角度进行深度分析：

1. 赔率分析：
   - 博彩公司对比赛的看法
   - 赔率反映的胜率概率
   - 是否存在投注价值

2. 伤停情况影响：
   {json.dumps(injury_info, ensure_ascii=False, indent=2) if injury_info else '暂无伤停信息'}

3. 比赛重要程度：
   - 根据未来比赛安排判断该场比赛的重要性
   - 是否可能影响球队的排兵布阵
   {json.dumps(future_matches, ensure_ascii=False, indent=2) if future_matches else '暂无未来比赛信息'}

4. 综合判断：
   - 整合所有信息的最终预测
   - 解释为什么媒体预测会出现分歧
   - 提供最可能的比赛结果

请按照以下JSON格式回答：
{{
    "odds_analysis": "赔率分析结果",
    "injury_impact": "伤停情况影响分析",
    "match_importance": "比赛重要程度分析",
    "final_prediction": "最终预测结果（主胜/平局/客胜）",
    "confidence_level": "置信度（1-10）",
    "reasoning": "详细推理过程",
    "media_divergence_reason": "媒体预测分歧的原因分析"
}}

请确保回答是有效的JSON格式，用中文回答。
"""
        return prompt

    def perform_deep_analysis(self, match_key: str, match_data: Dict,
                            predictions: List[Dict], consistency_info: Dict) -> Optional[Dict]:
        """对低一致性比赛进行深度分析"""

        self.logger.info(f"开始深度分析比赛: {match_key}")

        prompt = self.create_deep_analysis_prompt(match_key, match_data, predictions, consistency_info)

        messages = [
            {
                "role": "user",
                "content": prompt
            }
        ]

        result = self.client.chat_completion(messages, thread_id="deep-analysis")

        if result:
            try:
                deep_analysis = json.loads(result['content'])

                self.update_stats(deep_analyses_count=1)

                return {
                    'match_key': match_key,
                    'consistency_info': consistency_info,
                    'deep_analysis': deep_analysis,
                    'raw_response': result['content'],
                    'usage': result.get('usage', {}),
                    'analysis_time': datetime.now().isoformat()
                }

            except json.JSONDecodeError:
                self.logger.warning(f"深度分析JSON解析失败: {match_key}")
                return {
                    'match_key': match_key,
                    'consistency_info': consistency_info,
                    'deep_analysis': {'raw_text': result['content']},
                    'raw_response': result['content'],
                    'usage': result.get('usage', {}),
                    'analysis_time': datetime.now().isoformat()
                }
        else:
            self.logger.error(f"深度分析失败: {match_key}")
            return None

    def generate_wechat_article(self, all_matches_summary: Dict,
                              deep_analyses: List[Dict]) -> Optional[str]:
        """生成微信公众号推文"""

        self.logger.info("开始生成微信公众号推文...")

        # 构建推文内容提示词
        deep_analysis_text = ""
        if deep_analyses:
            deep_analysis_text = "\n特别关注的争议比赛：\n"
            for i, analysis in enumerate(deep_analyses, 1):
                match_key = analysis['match_key']
                deep_data = analysis.get('deep_analysis', {})
                if isinstance(deep_data, dict):
                    final_prediction = deep_data.get('final_prediction', '未知')
                    reasoning = deep_data.get('reasoning', '无详细分析')
                    deep_analysis_text += f"{i}. {match_key}: 最终预测{final_prediction}\n   原因：{reasoning[:100]}...\n"

        prompt = f"""
请根据以下足球比赛分析结果，写一篇微信公众号推文：

整体分析汇总：
{json.dumps(all_matches_summary, ensure_ascii=False, indent=2)}

{deep_analysis_text}

推文要求：
1. 标题要吸引人，包含关键信息
2. 开头要有引人入胜的导语
3. 内容要专业但易懂，适合大众阅读
4. 包含比赛分析要点和推荐
5. 结尾要有互动性，鼓励读者参与讨论
6. 总字数控制在800-1200字
7. 使用适当的emoji增加趣味性
8. 格式要适合微信公众号发布

推文结构建议：
- 标题（吸引眼球）
- 导语（本期焦点）
- 主要比赛分析
- 争议比赛深度解读
- 投注建议（仅供参考）
- 互动结尾

请直接输出完整的推文内容，不需要JSON格式。
"""

        messages = [
            {
                "role": "user",
                "content": prompt
            }
        ]

        result = self.client.chat_completion(messages, thread_id="wechat-article")

        if result:
            return result['content']
        else:
            self.logger.error("微信推文生成失败")
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
                prediction = match.get('result_prediction', '未知')
                analysis = match.get('analysis', '无分析')
                summary_content += f"比赛 {i}：{home} vs {away}\n预测：{prediction}\n分析：{analysis}\n\n"

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
            "combined_prediction": "综合预测结果",
            "confidence_level": "置信度",
            "analysis_summary": "分析总结"
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

    def save_analysis_results(self, analyses: List[Dict], final_summary: Dict,
                            consistency_results: Dict, deep_analyses: List[Dict],
                            wechat_article: str = None):
        """保存分析结果"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # 获取API统计
        api_stats = self.client.get_stats()

        # 保存详细分析结果
        detailed_file = self.analysis_dir / f"enhanced_analysis_{timestamp}.json"
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
                'final_summary': final_summary,
                'consistency_results': consistency_results,
                'deep_analyses': deep_analyses,
                'wechat_article': wechat_article
            }, f, ensure_ascii=False, indent=2)

        # 保存微信推文
        if wechat_article:
            wechat_file = self.analysis_dir / f"wechat_article_{timestamp}.txt"
            with open(wechat_file, 'w', encoding='utf-8') as f:
                f.write(wechat_article)

        # 保存可读的汇总报告
        summary_file = self.analysis_dir / f"enhanced_report_{timestamp}.txt"
        with open(summary_file, 'w', encoding='utf-8') as f:
            f.write(f"足球文章增强智能分析汇总报告\n")
            f.write("=" * 60 + "\n")
            f.write(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"分析文章数量：{len(analyses)} 篇\n")
            f.write(f"使用模型：{self.client.model}\n")
            f.write(f"并发线程：{self.max_workers} 个\n")
            f.write(f"API成功率：{api_stats['success_rate']:.1f}%\n")
            f.write(f"总Token使用：{self.stats['total_tokens_used']}\n")
            f.write(f"低一致性比赛：{self.stats['low_consistency_matches']} 场\n")
            f.write(f"深度分析数量：{self.stats['deep_analyses_count']} 场\n")
            f.write("=" * 60 + "\n\n")

            # 一致性分析结果
            f.write("📊 比赛预测一致性分析：\n\n")
            for match_key, consistency in consistency_results.items():
                f.write(f"🏆 {match_key}:\n")
                f.write(f"   预测数量: {consistency['total_predictions']}\n")
                f.write(f"   主要预测: {consistency['most_common_result']}\n")
                f.write(f"   一致性: {consistency['consistency_percentage']:.1f}%\n")
                f.write(f"   平均置信度: {consistency['average_confidence']:.1f}\n")
                f.write(f"   预测分布: {consistency['result_distribution']}\n\n")

            # 深度分析结果
            if deep_analyses:
                f.write("🔍 争议比赛深度分析：\n\n")
                for analysis in deep_analyses:
                    match_key = analysis['match_key']
                    deep_data = analysis.get('deep_analysis', {})
                    if isinstance(deep_data, dict):
                        f.write(f"⚽ {match_key}:\n")
                        f.write(f"   最终预测: {deep_data.get('final_prediction', '未知')}\n")
                        f.write(f"   置信度: {deep_data.get('confidence_level', '未知')}\n")
                        f.write(f"   赔率分析: {deep_data.get('odds_analysis', '无')}\n")
                        f.write(f"   伤停影响: {deep_data.get('injury_impact', '无')}\n")
                        f.write(f"   比赛重要性: {deep_data.get('match_importance', '无')}\n")
                        f.write(f"   分歧原因: {deep_data.get('media_divergence_reason', '无')}\n")
                        f.write(f"   推理过程: {deep_data.get('reasoning', '无')}\n\n")

            # 写入最终汇总
            if 'summary_json' in final_summary:
                summary_data = final_summary['summary_json']
                if isinstance(summary_data, dict):
                    f.write("📈 综合分析结果：\n\n")

                    if 'unique_matches' in summary_data:
                        f.write("🏆 所有比赛综合预测：\n")
                        for i, match in enumerate(summary_data['unique_matches'], 1):
                            if isinstance(match, dict):
                                f.write(f"{i}. {match.get('home_team', '未知')} vs {match.get('away_team', '未知')}\n")
                                f.write(f"   预测：{match.get('combined_prediction', '无预测')}\n")
                                f.write(f"   置信度：{match.get('confidence_level', '未知')}\n")
                                f.write(f"   分析：{match.get('analysis_summary', '无分析')}\n\n")

                    if 'overall_insights' in summary_data:
                        f.write(f"💡 整体洞察：\n{summary_data['overall_insights']}\n\n")

                    if 'betting_suggestions' in summary_data:
                        f.write(f"💰 投注建议：\n{summary_data['betting_suggestions']}\n\n")

        self.logger.info(f"增强分析结果已保存:")
        self.logger.info(f"  详细数据: {detailed_file}")
        self.logger.info(f"  汇总报告: {summary_file}")
        if wechat_article:
            self.logger.info(f"  微信推文: {wechat_file}")

        return detailed_file, summary_file

    def run_enhanced_analysis(self, match_filter: str = None, resume: bool = False):
        """运行增强版分析流程（包含一致性检查和深度分析）"""
        self.stats['start_time'] = datetime.now().isoformat()

        self.logger.info("=== 足球文章增强智能分析器启动 ===")
        self.logger.info(f"配置信息: {self.max_workers}线程, 最大重试{ANALYSIS_CONFIG['max_retries']}次")
        self.logger.info(f"API地址: {self.client.base_url}")
        self.logger.info(f"使用模型: {self.client.model}")

        # 加载比赛基础数据
        match_data_dict = self.load_match_data()

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

        if not articles and not progress['analyses']:
            self.logger.error("没有文章需要分析")
            return

        # 如果有新文章需要分析，进行批量分析
        if articles:
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
            self.logger.error("没有成功分析的文章，无法进行后续处理")
            return

        # 检查一致性
        self.logger.info("开始一致性检查...")
        consistency_results = self.check_match_consistency(progress['analyses'])

        # 识别需要深度分析的比赛（一致性低于70%）
        low_consistency_matches = {
            match_key: info for match_key, info in consistency_results.items()
            if info['consistency_percentage'] < 70.0
        }

        self.update_stats(low_consistency_matches=len(low_consistency_matches))

        if low_consistency_matches:
            self.logger.info(f"发现 {len(low_consistency_matches)} 场低一致性比赛，开始深度分析...")

            deep_analyses = []
            for match_key, consistency_info in low_consistency_matches.items():
                # 查找对应的比赛数据
                match_data = None
                for key, data in match_data_dict.items():
                    if match_key in key or key in match_key:
                        match_data = data
                        break

                if match_data:
                    deep_analysis = self.perform_deep_analysis(
                        match_key, match_data,
                        consistency_info['predictions'],
                        consistency_info
                    )
                    if deep_analysis:
                        deep_analyses.append(deep_analysis)
                else:
                    self.logger.warning(f"未找到比赛 {match_key} 的足彩数据")
        else:
            self.logger.info("所有比赛预测一致性良好，无需深度分析")
            deep_analyses = []

        # 进行最终汇总
        self.logger.info("正在进行最终汇总分析...")
        final_summary = self.summarize_all_analyses(progress['analyses'])

        if not final_summary:
            self.logger.error("最终汇总失败")
            return

        # 生成微信推文
        self.logger.info("正在生成微信公众号推文...")
        wechat_article = self.generate_wechat_article(
            final_summary.get('summary_json', {}),
            deep_analyses
        )

        # 保存所有结果
        detailed_file, summary_file = self.save_analysis_results(
            progress['analyses'], final_summary, consistency_results,
            deep_analyses, wechat_article
        )

        # 显示汇总结果
        print("\n" + "=" * 60)
        print("📋 增强分析结果汇总：")
        print("=" * 60)
        print(f"📖 分析文章: {len(progress['analyses'])} 篇")
        print(f"🏆 发现比赛: {len(consistency_results)} 场")
        print(f"⚠️  争议比赛: {len(low_consistency_matches)} 场")
        print(f"🔍 深度分析: {len(deep_analyses)} 场")

        if deep_analyses:
            print(f"\n🔥 争议比赛详情:")
            for analysis in deep_analyses:
                match_key = analysis['match_key']
                deep_data = analysis.get('deep_analysis', {})
                if isinstance(deep_data, dict):
                    final_pred = deep_data.get('final_prediction', '未知')
                    confidence = deep_data.get('confidence_level', '未知')
                    print(f"   {match_key}: {final_pred} (置信度:{confidence})")

        if wechat_article:
            print(f"\n📱 微信推文已生成 ({len(wechat_article)} 字符)")
            print("=" * 60)
            print(wechat_article[:300] + "..." if len(wechat_article) > 300 else wechat_article)

        print("=" * 60)

        # 清除进度文件
        if progress_file.exists():
            progress_file.unlink()
            self.logger.info("已清除进度文件")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='足球文章增强智能分析器')
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

    print("=== 足球文章增强智能分析器 ===")

    # 获取API Token
    api_token = args.token
    if not api_token:
        if API_CONFIG["api_token"] and API_CONFIG["api_token"] != "sk-":
            api_token = API_CONFIG["api_token"]
            print("使用配置文件中的API Token")
        else:
            api_token = input("请输入API Token: ").strip()

    if not api_token:
        print("API Token不能为空")
        return

    # 设置线程数
    if args.workers:
        ANALYSIS_CONFIG["max_workers"] = args.workers
        print(f"设置线程数为: {args.workers}")

    try:
        # 创建分析器并运行
        analyzer = AdvancedMultiThreadArticleAnalyzer(api_token)
        analyzer.run_enhanced_analysis(match_filter=args.filter, resume=args.resume)

    except KeyboardInterrupt:
        print("\n\n用户中断了程序")
        print("当前进度已保存，可使用 --resume 参数继续")
    except Exception as e:
        print(f"\n程序运行出错: {e}")
        import traceback
        traceback.print_exc()

    print("\n程序结束")


if __name__ == "__main__":
    main()