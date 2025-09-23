import requests
import json


def search_web(query, api_key, count=10, summary=False, freshness=None):
    """
    执行网页搜索

    参数:
        query (str): 搜索关键词
        api_key (str): API密钥
        count (int): 返回结果数量，默认10
        offset (int): 搜索页码
    """
    url = "https://platform.kuaisou.com/api/web-search"

    # 构建请求数据
    payload = json.dumps({
        "query": query,
        "offset": 1,
        "count": count,
        "freshness": freshness
    })

    # 设置请求头
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json'
    }

    # 发送请求
    response = requests.post(url, headers=headers, data=payload)

    # 检查响应状态
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(f"搜索请求失败: {response.status_code} - {response.text}")


# 使用示例
if __name__ == "__main__":
    # 设置API密钥
    API_KEY = "ks_ug5tYT7FzE9CimlJu0vEY0eYJoz6tbvojtsa36FLOEaOppBl"  # 替换为您的API密钥

    try:
        # 执行搜索
        results = search_web(
            query="大阪钢巴VS横滨水手 前瞻",
            api_key=API_KEY,
            count=100,
            summary=True,
            freshness = "2025-09-22..2025-09-23"
        )

        # 打印结果
        print(json.dumps(results, indent=2, ensure_ascii=False))

    except Exception as e:
        print(f"发生错误: {str(e)}")