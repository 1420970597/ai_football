#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
浏览器抓取客户端
与沙盒浏览器服务进行交互的客户端
"""

import requests
import logging
from typing import Dict, List, Optional, Any
import time


class BrowserScraperClient:
    """浏览器抓取客户端"""
    
    def __init__(self, service_url: str = "http://localhost:8080"):
        self.service_url = service_url.rstrip('/')
        self.session = requests.Session()
        self.session.timeout = 60
        
        self.logger = logging.getLogger(__name__)
    
    def check_health(self) -> bool:
        """检查服务健康状态"""
        try:
            response = self.session.get(f"{self.service_url}/health")
            return response.status_code == 200
        except Exception as e:
            self.logger.error(f"健康检查失败: {e}")
            return False
    
    def scrape_page(self, url: str, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """抓取单个页面"""
        try:
            data = {
                'url': url,
                'config': config or {}
            }
            
            response = self.session.post(
                f"{self.service_url}/scrape",
                json=data
            )
            
            if response.status_code == 200:
                return response.json()
            else:
                return {
                    'success': False,
                    'error': f'HTTP {response.status_code}: {response.text}',
                    'url': url
                }
                
        except Exception as e:
            self.logger.error(f"抓取请求失败: {url}, {e}")
            return {
                'success': False,
                'error': f'请求失败: {str(e)}',
                'url': url
            }
    
    def scrape_multiple(self, urls: List[str], config: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        """批量抓取多个页面"""
        try:
            data = {
                'urls': urls,
                'config': config or {}
            }
            
            response = self.session.post(
                f"{self.service_url}/scrape/batch",
                json=data
            )
            
            if response.status_code == 200:
                result = response.json()
                return result.get('results', [])
            else:
                # 如果批量失败，回退到单个抓取
                self.logger.warning(f"批量抓取失败，回退到单个抓取: {response.status_code}")
                return [self.scrape_page(url, config) for url in urls]
                
        except Exception as e:
            self.logger.error(f"批量抓取失败: {e}")
            # 回退到单个抓取
            return [self.scrape_page(url, config) for url in urls]
    
    def scrape_with_retry(self, url: str, config: Dict[str, Any] = None, max_retries: int = 3) -> Dict[str, Any]:
        """带重试的抓取"""
        for attempt in range(max_retries):
            result = self.scrape_page(url, config)
            
            if result.get('success'):
                return result
            
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # 指数退避
                self.logger.info(f"抓取失败，{wait_time}秒后重试: {url}")
                time.sleep(wait_time)
            else:
                self.logger.error(f"抓取最终失败: {url}")
        
        return result