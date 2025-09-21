# 足球文章智能分析器使用说明

## 简介
足球文章智能分析器可以自动分析output文件夹中的足球文章，使用硅基流动大模型提取比赛信息和分析结果，并生成综合汇总报告。

## 文件说明

### 1. article_analyzer.py（基础版）
- 基本的文章分析功能
- 交互式输入API Token
- 适合初次使用和简单分析

### 2. advanced_analyzer.py（高级版）⭐ 推荐
- 支持配置文件管理
- 断点续传功能
- 批量处理和过滤
- 详细的统计信息
- JSON格式化输出

### 3. config.py（配置文件）
- API Token配置
- 模型参数设置
- 分析选项配置

## 快速开始

### 第一步：配置API Token
1. 编辑 `config.py` 文件
2. 在 `SILICONFLOW_CONFIG["api_token"]` 中填入你的API Token

```python
SILICONFLOW_CONFIG = {
    "api_token": "你的API_Token",  # 在这里填入
    "model": "Qwen/QwQ-32B",
    # ... 其他配置
}
```

### 第二步：运行分析
#### 基础版使用方法：
```bash
python article_analyzer.py
```

#### 高级版使用方法：
```bash
# 基本使用
python advanced_analyzer.py

# 指定API Token
python advanced_analyzer.py --token "你的API_Token"

# 过滤特定比赛
python advanced_analyzer.py --filter "桑德兰"

# 断点续传
python advanced_analyzer.py --resume
```

## 功能特性

### 📖 文章分析
- 自动扫描 `output/articles/` 目录下的所有文章
- 提取文章标题、摘要和正文内容
- 使用AI大模型分析比赛信息

### 🏆 比赛识别
- 识别文章中提到的足球比赛（主队 vs 客队）
- 提取每场比赛的分析结果
- 包括比分预测、优势分析、关键因素等

### 📊 智能汇总
- 收集所有单篇文章的分析结果
- 去重并合并相同比赛的分析
- 生成综合性的分析报告

### 💾 结果保存
分析结果保存在 `output/analysis/` 目录下：
- `detailed_analysis_时间戳.json` - 详细的JSON数据
- `summary_report_时间戳.txt` - 可读的汇总报告

### 🔄 断点续传
- 支持中断后继续分析（仅高级版）
- 自动保存分析进度
- 避免重复分析已完成的文章

## 输出示例

### 控制台输出：
```
=== 足球文章高级智能分析器启动 ===

🔍 正在查找文章文件...
✅ 需要分析 15 篇文章

📖 开始分析文章内容...

--- 分析第 1/15 篇文章 ---
  正在分析文章: 桑德兰vs维拉：本轮英超焦点对决分析...
    ✅ 分析成功

📊 文章分析统计:
  ✅ 成功分析: 14 篇
  ❌ 分析失败: 1 篇
  🔤 使用Token: 25680

🔄 正在进行最终汇总分析...
✅ 汇总分析完成

📋 最终汇总分析结果：
🏆 比赛分析：
1. 桑德兰 vs 维拉
   根据综合分析，维拉在攻击力和整体实力上占据优势...
```

### JSON输出格式：
```json
{
  "matches": [
    {
      "home_team": "桑德兰",
      "away_team": "维拉",
      "analysis": "详细的比赛分析..."
    }
  ],
  "summary": "文章整体分析总结",
  "confidence": "高"
}
```

## 配置说明

### API配置：
```python
SILICONFLOW_CONFIG = {
    "api_token": "",          # API Token
    "model": "Qwen/QwQ-32B", # 使用的模型
    "max_tokens": 2000,      # 最大输出长度
    "temperature": 0.7       # 创造性参数
}
```

### 分析配置：
```python
ANALYSIS_CONFIG = {
    "max_content_length": 2000,  # 文章内容最大长度
    "request_delay": 1,          # API请求间隔（秒）
    "max_retries": 3,            # 最大重试次数
    "timeout": 60                # 请求超时时间（秒）
}
```

## 注意事项

1. **API费用**：使用大模型API会产生费用，请根据需要控制分析数量
2. **网络连接**：需要稳定的网络连接访问硅基流动API
3. **文章质量**：只分析成功提取内容的文章文件
4. **Token限制**：单次分析的文章内容会被截断到指定长度
5. **频率限制**：API调用有频率限制，程序会自动添加延迟

## 故障排除

### 常见问题：
1. **API Token错误**：检查config.py中的token是否正确
2. **网络连接失败**：检查网络连接和API服务状态
3. **没有找到文章**：确保output/articles目录下有有效的文章文件
4. **JSON解析失败**：AI返回的结果可能不是标准JSON，程序会保存原始响应

### 调试方法：
- 查看详细的错误信息
- 检查保存的JSON文件中的raw_response字段
- 使用--filter参数先分析少量文章进行测试

## 更新日志

### v2.0（高级版）
- 添加配置文件支持
- 实现断点续传功能
- 增加批量处理和过滤
- 优化JSON格式输出
- 添加详细统计信息

### v1.0（基础版）
- 基本的文章分析功能
- 大模型API集成
- 简单的汇总报告生成

---

如有问题或建议，请检查代码注释或联系开发者。