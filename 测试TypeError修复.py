#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
快速修复TypeError测试脚本
"""

import sys
import os

# 添加项目根目录到Python路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from main import FootballAnalyzer

def test_generate_wechat_post():
    """测试generate_wechat_post函数的类型错误"""
    print("=== 测试TypeError修复 ===")
    
    analyzer = FootballAnalyzer()
    
    # 创建测试数据
    test_match_data = {
        '基本信息': {
            '主队名称': '测试主队',
            '客队名称': '测试客队',
            '比赛时间': '2025-09-22 18:30:00',
            '联赛名称': '测试联赛'
        },
        '赔率信息': {
            '胜平负格式': '2.5 3.2 2.8'
        }
    }
    
    test_analyses = [
        {
            'analysis_result': {
                'summary': '这是一场势均力敌的比赛'
            }
        }
    ]
    
    # 测试不同类型的consistency_data
    test_cases = [
        ("空字典", {}),
        ("None值", None),
        ("整数", 123),  # 这个应该会被修复
        ("字符串", "test"),  # 这个也应该会被修复
        ("正常字典", {
            'total_predictions': 4,
            'most_common_result': '主胜',
            'consistency_percentage': 75.0,
            'average_confidence': 7.5
        })
    ]
    
    for test_name, consistency_data in test_cases:
        print(f"\n测试案例: {test_name}")
        print(f"输入类型: {type(consistency_data)}")
        print(f"输入值: {consistency_data}")
        
        try:
            result = analyzer.generate_wechat_post(test_match_data, consistency_data, test_analyses)
            print(f"[成功] 生成推文成功，长度: {len(result)} 字符")
            if "一致性" in result:
                print(f"[检查] 推文包含一致性信息")
            else:
                print(f"[检查] 推文不包含一致性信息（正常）")
                
        except Exception as e:
            print(f"[失败] 生成推文失败: {e}")
            print(f"[错误类型] {type(e).__name__}")
    
    print(f"\n=== TypeError修复测试完成 ===")
    print("如果所有测试案例都成功，说明TypeError已修复")

if __name__ == "__main__":
    test_generate_wechat_post()