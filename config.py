#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置文件
"""

# API配置
API_CONFIG = {
    "api_token": "sk-oeyzbmbxzmwxxxqwqnarnokprpglwbppsnyzqoyrfdikedrc",  # API Token
    "model": "moonshotai/Kimi-K2-Instruct-0905",
    "base_url": "https://api.siliconflow.cn/v1/chat/completions",
    "max_tokens": 200000,
    "temperature": 0.7
}

# 分析配置
ANALYSIS_CONFIG = {
    "max_content_length": 300000,  # 文章内容最大长度
    "request_delay": 0.1,  # API请求间隔（秒）
    "max_retries": 10,  # 最大重试次数
    "timeout": 120,  # 请求超时时间（秒）
    "max_workers": 50,  # 最大线程数
    "batch_size": 10  # 批处理大小
}

# 兼容旧配置（保持向后兼容）
SILICONFLOW_CONFIG = API_CONFIG

# SportsDataIO API配置
SPORTSDATA_CONFIG = {
    "api_key": "YOUR_SPORTSDATA_API_KEY"  # 在这里替换为你的SportsData.io API密钥
}