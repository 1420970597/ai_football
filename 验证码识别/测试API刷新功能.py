#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证码识别API刷新功能测试示例
当API返回code 10007时，程序会自动刷新页面重新获取验证码
"""

# 模拟API返回的不同情况
test_cases = [
    {
        "name": "成功识别",
        "response": {
            "msg": "识别成功", 
            "code": 10000, 
            "data": {"code": 0, "data": "88,98|36,67|217,52|157,124", "time": 0.095}
        },
        "expected_action": "解析坐标并执行点击"
    },
    {
        "name": "图片识别失败",
        "response": {
            "msg": "图片未识别成功，请换图重试或联系客服咨询其他识别接口！", 
            "code": 10007, 
            "data": []
        },
        "expected_action": "刷新页面重新获取验证码"
    },
    {
        "name": "其他错误",
        "response": {
            "msg": "余额不足", 
            "code": 10001, 
            "data": []
        },
        "expected_action": "记录错误，继续重试"
    }
]

print("=== 验证码识别API响应处理逻辑 ===\n")

for i, case in enumerate(test_cases, 1):
    print(f"{i}. {case['name']}")
    print(f"   API响应: {case['response']}")
    print(f"   程序动作: {case['expected_action']}")
    print()

print("=== 新增功能说明 ===")
print("1. call_captcha_api函数现在返回 (coordinates, api_code) 元组")
print("2. auto_solve_captcha函数检测到api_code=10007时会:")
print("   - 打印'[刷新] 图片识别失败，刷新页面重新获取验证码...'")
print("   - 执行 self.driver.refresh() 刷新页面")
print("   - 等待3秒让页面重新加载")
print("   - continue到下一次循环重新尝试")
print("3. 功能7的演示模式也会显示对应的提示信息")