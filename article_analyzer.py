#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
足球文章智能分析器
自动分析output中的文章内容，使用硅基流动大模型进行比赛分析
"""

import os
import json
import time
import requests
from datetime import datetime
from typing import List, Dict, Optional
from pathlib import Path


class SiliconFlowClient:
    """硅基流动API客户端"""

    def __init__(self, api_token: str):
        self.api_token = api_token
        self.base_url = "https://api.siliconflow.cn/v1/chat/completions"
        self.model = "Qwen/QwQ-32B"
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }

    def chat_completion(self, messages: List[Dict], max_retries: int = 3) -> Optional[str]:
        """调用大模型进行对话"""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 2000
        }

        for attempt in range(max_retries):
            try:
                response = requests.post(
                    self.base_url,
                    json=payload,
                    headers=self.headers,
                    timeout=60
                )
                response.raise_for_status()

                result = response.json()
                if 'choices' in result and len(result['choices']) > 0:
                    return result['choices'][0]['message']['content']
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


class ArticleAnalyzer:
    """文章分析器"""

    def __init__(self, api_token: str):
        self.client = SiliconFlowClient(api_token)
        self.output_dir = Path("output")
        self.articles_dir = self.output_dir / "articles"
        self.analysis_dir = self.output_dir / "analysis"

        # 创建分析结果目录
        self.analysis_dir.mkdir(exist_ok=True)

    def find_all_articles(self) -> List[Dict]:
        """找到所有文章JSON文件"""
        articles = []

        if not self.articles_dir.exists():
            print("❌ 文章目录不存在")
            return articles

        for match_folder in self.articles_dir.iterdir():
            if match_folder.is_dir() and match_folder.name.startswith("match_"):
                # 解析比赛信息
                try:
                    match_info_file = match_folder / "match_info.json"
                    if match_info_file.exists():
                        with open(match_info_file, 'r', encoding='utf-8') as f:
                            match_info = json.load(f)
                except Exception as e:
                    print(f"读取比赛信息失败 {match_folder.name}: {e}")
                    continue

                # 查找所有文章文件
                for article_file in match_folder.glob("article_*.json"):
                    try:
                        with open(article_file, 'r', encoding='utf-8') as f:
                            article_data = json.load(f)

                        articles.append({
                            'match_folder': match_folder.name,
                            'article_file': article_file.name,
                            'file_path': str(article_file),
                            'match_info': match_info,
                            'article_data': article_data
                        })
                    except Exception as e:
                        print(f"读取文章文件失败 {article_file}: {e}")

        return articles

    def analyze_single_article(self, article: Dict) -> Optional[Dict]:
        """分析单篇文章"""
        article_info = article['article_data'].get('article_info', {})
        content_data = article['article_data'].get('content_data', {})

        title = article_info.get('title', '无标题')
        summary = article_info.get('summary', '无摘要')
        text_content = content_data.get('text', '无内容')

        # 构建分析提示词
        prompt = f"""
请分析以下足球相关文章，提取其中的比赛信息和分析结果：

文章标题：{title}
文章摘要：{summary}
文章内容：{text_content[:2000]}{"..." if len(text_content) > 2000 else ""}

请按照以下格式回答：
1. 文章中提到了哪几场比赛？（格式：主队 vs 客队）
2. 对每场比赛的分析结果是什么？（包括比分预测、优势分析、关键因素等）

请用中文回答，格式要清晰。
"""

        messages = [
            {
                "role": "user",
                "content": prompt
            }
        ]

        print(f"  正在分析文章: {title[:30]}...")

        analysis_result = self.client.chat_completion(messages)

        if analysis_result:
            return {
                'article_info': {
                    'title': title,
                    'summary': summary,
                    'url': article_info.get('url', ''),
                    'publish_time': article_info.get('publish_time', ''),
                    'file_path': article['file_path']
                },
                'analysis_result': analysis_result,
                'analysis_time': datetime.now().isoformat()
            }
        else:
            print(f"    ❌ 分析失败")
            return None

    def summarize_all_analyses(self, analyses: List[Dict]) -> Optional[str]:
        """汇总所有分析结果"""
        if not analyses:
            return None

        # 构建汇总内容
        summary_content = "以下是对多篇足球文章的分析结果汇总：\n\n"

        for i, analysis in enumerate(analyses, 1):
            summary_content += f"文章 {i}：{analysis['article_info']['title']}\n"
            summary_content += f"分析结果：{analysis['analysis_result']}\n"
            summary_content += "-" * 50 + "\n\n"

        # 构建最终汇总提示词
        final_prompt = f"""
请对以下多篇足球文章的分析结果进行综合汇总分析：

{summary_content}

请提供一个综合性的总结，包括：
1. 所有提到的比赛汇总（去重）
2. 各场比赛的综合分析结论
3. 整体趋势和关键洞察
4. 投注建议（如果有的话）

请用中文回答，条理清晰，重点突出。
"""

        messages = [
            {
                "role": "user",
                "content": final_prompt
            }
        ]

        print("🤖 正在进行最终汇总分析...")

        return self.client.chat_completion(messages)

    def save_analysis_results(self, analyses: List[Dict], final_summary: str):
        """保存分析结果"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # 保存详细分析结果
        detailed_file = self.analysis_dir / f"detailed_analysis_{timestamp}.json"
        with open(detailed_file, 'w', encoding='utf-8') as f:
            json.dump({
                'analysis_time': datetime.now().isoformat(),
                'total_articles': len(analyses),
                'detailed_analyses': analyses
            }, f, ensure_ascii=False, indent=2)

        # 保存最终汇总
        summary_file = self.analysis_dir / f"final_summary_{timestamp}.txt"
        with open(summary_file, 'w', encoding='utf-8') as f:
            f.write(f"足球文章分析汇总报告\n")
            f.write(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"分析文章数量：{len(analyses)} 篇\n")
            f.write("=" * 60 + "\n\n")
            f.write(final_summary)

        print(f"📁 分析结果已保存:")
        print(f"  详细分析: {detailed_file}")
        print(f"  最终汇总: {summary_file}")

    def run_analysis(self):
        """运行完整的分析流程"""
        print("=== 足球文章智能分析器启动 ===\n")

        # 查找所有文章
        print("🔍 正在查找文章文件...")
        articles = self.find_all_articles()

        if not articles:
            print("❌ 没有找到任何文章文件")
            return

        print(f"✅ 找到 {len(articles)} 篇文章")

        # 分析每篇文章
        print("\n📖 开始分析文章内容...")
        successful_analyses = []
        failed_count = 0

        for i, article in enumerate(articles, 1):
            print(f"\n--- 分析第 {i}/{len(articles)} 篇文章 ---")

            analysis = self.analyze_single_article(article)
            if analysis:
                successful_analyses.append(analysis)
                print(f"    ✅ 分析成功")
            else:
                failed_count += 1

            # 添加延迟避免API限制
            time.sleep(1)

        print(f"\n📊 文章分析统计:")
        print(f"  ✅ 成功分析: {len(successful_analyses)} 篇")
        print(f"  ❌ 分析失败: {failed_count} 篇")

        if not successful_analyses:
            print("❌ 没有成功分析的文章，无法进行汇总")
            return

        # 进行最终汇总
        print("\n🔄 正在进行最终汇总分析...")
        final_summary = self.summarize_all_analyses(successful_analyses)

        if final_summary:
            print("✅ 汇总分析完成")

            # 保存结果
            self.save_analysis_results(successful_analyses, final_summary)

            # 显示汇总结果
            print("\n" + "=" * 60)
            print("📋 最终汇总分析结果：")
            print("=" * 60)
            print(final_summary)
            print("=" * 60)
        else:
            print("❌ 最终汇总失败")


def main():
    """主函数"""
    print("足球文章智能分析器")
    print("请确保已设置正确的API Token")

    # 获取API Token
    api_token = input("\n请输入硅基流动API Token: ").strip()

    if not api_token:
        print("❌ API Token不能为空")
        return

    # 创建分析器并运行
    analyzer = ArticleAnalyzer(api_token)

    try:
        analyzer.run_analysis()
    except KeyboardInterrupt:
        print("\n\n⚠️ 用户中断了程序")
    except Exception as e:
        print(f"\n❌ 程序运行出错: {e}")

    print("\n程序结束")


if __name__ == "__main__":
    main()