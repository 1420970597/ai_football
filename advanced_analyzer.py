#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
足球文章高级智能分析器
支持配置文件、批量处理、断点续传等功能
"""

import os
import json
import time
import requests
from datetime import datetime
from typing import List, Dict, Optional
from pathlib import Path
import argparse

# 尝试导入配置文件
try:
    from config import SILICONFLOW_CONFIG, ANALYSIS_CONFIG
except ImportError:
    print("警告：未找到config.py文件，将使用默认配置")
    SILICONFLOW_CONFIG = {
        "api_token": "",
        "model": "Qwen/QwQ-32B",
        "base_url": "https://api.siliconflow.cn/v1/chat/completions",
        "max_tokens": 2000,
        "temperature": 0.7
    }
    ANALYSIS_CONFIG = {
        "max_content_length": 2000,
        "request_delay": 1,
        "max_retries": 3,
        "timeout": 60
    }


class AdvancedSiliconFlowClient:
    """硅基流动API客户端（增强版）"""

    def __init__(self, api_token: str = None):
        self.api_token = api_token or SILICONFLOW_CONFIG["api_token"]
        self.base_url = SILICONFLOW_CONFIG["base_url"]
        self.model = SILICONFLOW_CONFIG["model"]
        self.max_tokens = SILICONFLOW_CONFIG["max_tokens"]
        self.temperature = SILICONFLOW_CONFIG["temperature"]

        if not self.api_token:
            raise ValueError("API Token未设置，请在config.py中配置或作为参数传入")

        self.headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }

    def chat_completion(self, messages: List[Dict],
                       max_retries: int = None,
                       timeout: int = None) -> Optional[Dict]:
        """调用大模型进行对话（返回完整响应）"""
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
                response = requests.post(
                    self.base_url,
                    json=payload,
                    headers=self.headers,
                    timeout=timeout
                )
                response.raise_for_status()

                result = response.json()
                if 'choices' in result and len(result['choices']) > 0:
                    return {
                        'content': result['choices'][0]['message']['content'],
                        'usage': result.get('usage', {}),
                        'model': result.get('model', self.model)
                    }
                else:
                    print(f"API响应格式异常: {result}")
                    return None

            except requests.exceptions.RequestException as e:
                print(f"API调用失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)  # 指数退避
                else:
                    print("API调用最终失败")
                    return None
            except Exception as e:
                print(f"未知错误: {e}")
                return None

        return None


class AdvancedArticleAnalyzer:
    """文章分析器（增强版）"""

    def __init__(self, api_token: str = None):
        self.client = AdvancedSiliconFlowClient(api_token)
        self.output_dir = Path("output")
        self.articles_dir = self.output_dir / "articles"
        self.analysis_dir = self.output_dir / "analysis"

        # 创建分析结果目录
        self.analysis_dir.mkdir(exist_ok=True)

        # 统计信息
        self.stats = {
            'total_articles': 0,
            'successful_analyses': 0,
            'failed_analyses': 0,
            'total_tokens_used': 0,
            'start_time': None,
            'end_time': None
        }

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
        """保存分析进度"""
        try:
            with open(progress_file, 'w', encoding='utf-8') as f:
                json.dump(progress, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存进度失败: {e}")

    def find_all_articles(self, match_filter: str = None) -> List[Dict]:
        """找到所有文章JSON文件"""
        articles = []

        if not self.articles_dir.exists():
            print("❌ 文章目录不存在")
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
                print(f"读取比赛信息失败 {match_folder.name}: {e}")
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
                    print(f"读取文章文件失败 {article_file}: {e}")

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

    def analyze_single_article(self, article: Dict) -> Optional[Dict]:
        """分析单篇文章"""
        article_info = article['article_data'].get('article_info', {})
        content_data = article['article_data'].get('content_data', {})

        title = article_info.get('title', '无标题')

        # 创建分析提示词
        prompt = self.create_analysis_prompt(article_info, content_data)

        messages = [
            {
                "role": "user",
                "content": prompt
            }
        ]

        print(f"  正在分析文章: {title[:50]}...")

        result = self.client.chat_completion(messages)

        if result:
            # 尝试解析JSON响应
            try:
                analysis_json = json.loads(result['content'])

                return {
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
                    'analysis_time': datetime.now().isoformat()
                }
            except json.JSONDecodeError:
                print(f"    ⚠️ JSON解析失败，保存原始响应")
                return {
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
                    'analysis_time': datetime.now().isoformat()
                }
        else:
            print(f"    ❌ 分析失败")
            return None

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

        print("🤖 正在进行最终汇总分析...")

        result = self.client.chat_completion(messages)
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

        # 保存详细分析结果
        detailed_file = self.analysis_dir / f"detailed_analysis_{timestamp}.json"
        with open(detailed_file, 'w', encoding='utf-8') as f:
            json.dump({
                'metadata': {
                    'analysis_time': datetime.now().isoformat(),
                    'total_articles': len(analyses),
                    'model_used': self.client.model,
                    'stats': self.stats
                },
                'detailed_analyses': analyses,
                'final_summary': final_summary
            }, f, ensure_ascii=False, indent=2)

        # 保存可读的汇总报告
        summary_file = self.analysis_dir / f"summary_report_{timestamp}.txt"
        with open(summary_file, 'w', encoding='utf-8') as f:
            f.write(f"足球文章智能分析汇总报告\n")
            f.write("=" * 60 + "\n")
            f.write(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"分析文章数量：{len(analyses)} 篇\n")
            f.write(f"使用模型：{self.client.model}\n")
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

        print(f"📁 分析结果已保存:")
        print(f"  详细数据: {detailed_file}")
        print(f"  汇总报告: {summary_file}")

        return detailed_file, summary_file

    def run_analysis(self, match_filter: str = None, resume: bool = False):
        """运行完整的分析流程"""
        self.stats['start_time'] = datetime.now().isoformat()

        print("=== 足球文章高级智能分析器启动 ===\n")

        # 设置进度文件
        progress_file = self.analysis_dir / "analysis_progress.json"
        progress = self.load_progress(progress_file) if resume else {'completed_files': [], 'analyses': []}

        # 查找所有文章
        print("🔍 正在查找文章文件...")
        articles = self.find_all_articles(match_filter)

        if not articles:
            print("❌ 没有找到任何文章文件")
            return

        # 过滤已完成的文章（如果是断点续传）
        if resume and progress['completed_files']:
            articles = [a for a in articles if a['relative_path'] not in progress['completed_files']]
            print(f"📄 断点续传：跳过已完成的 {len(progress['completed_files'])} 篇文章")

        self.stats['total_articles'] = len(articles)
        print(f"✅ 需要分析 {len(articles)} 篇文章")

        if not articles:
            print("✅ 所有文章都已分析完成")
            if progress['analyses']:
                print("🔄 开始最终汇总...")
                final_summary = self.summarize_all_analyses(progress['analyses'])
                if final_summary:
                    self.save_analysis_results(progress['analyses'], final_summary)
            return

        # 分析每篇文章
        print("\n📖 开始分析文章内容...")

        for i, article in enumerate(articles, 1):
            print(f"\n--- 分析第 {i}/{len(articles)} 篇文章 ---")

            analysis = self.analyze_single_article(article)
            if analysis:
                progress['analyses'].append(analysis)
                progress['completed_files'].append(article['relative_path'])
                self.stats['successful_analyses'] += 1

                # 统计token使用
                if 'usage' in analysis:
                    self.stats['total_tokens_used'] += analysis['usage'].get('total_tokens', 0)

                print(f"    ✅ 分析成功")
            else:
                self.stats['failed_analyses'] += 1

            # 保存进度
            self.save_progress(progress_file, progress)

            # 添加延迟避免API限制
            time.sleep(ANALYSIS_CONFIG["request_delay"])

        self.stats['end_time'] = datetime.now().isoformat()

        print(f"\n📊 文章分析统计:")
        print(f"  ✅ 成功分析: {self.stats['successful_analyses']} 篇")
        print(f"  ❌ 分析失败: {self.stats['failed_analyses']} 篇")
        print(f"  🔤 使用Token: {self.stats['total_tokens_used']}")

        if not progress['analyses']:
            print("❌ 没有成功分析的文章，无法进行汇总")
            return

        # 进行最终汇总
        print("\n🔄 正在进行最终汇总分析...")
        final_summary = self.summarize_all_analyses(progress['analyses'])

        if final_summary:
            print("✅ 汇总分析完成")

            # 更新token统计
            if 'usage' in final_summary:
                self.stats['total_tokens_used'] += final_summary['usage'].get('total_tokens', 0)

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
                print("🗑️ 已清除进度文件")

        else:
            print("❌ 最终汇总失败")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='足球文章高级智能分析器')
    parser.add_argument('--token', type=str, help='硅基流动API Token')
    parser.add_argument('--filter', type=str, help='比赛文件夹过滤关键词')
    parser.add_argument('--resume', action='store_true', help='断点续传')

    args = parser.parse_args()

    print("=== 足球文章高级智能分析器 ===")

    # 获取API Token
    api_token = args.token
    if not api_token:
        if SILICONFLOW_CONFIG["api_token"]:
            api_token = SILICONFLOW_CONFIG["api_token"]
            print("✅ 使用配置文件中的API Token")
        else:
            api_token = input("请输入硅基流动API Token: ").strip()

    if not api_token:
        print("❌ API Token不能为空")
        return

    try:
        # 创建分析器并运行
        analyzer = AdvancedArticleAnalyzer(api_token)
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