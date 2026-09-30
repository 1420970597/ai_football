# ai-analyzer —— 分析编排与 LLM 调用层（B/S 架构的 Server 侧业务容器）
#
# 构建： docker compose -f docker/docker-compose.yml build ai-analyzer
# 说明： 本镜像只承载「分析编排」职责，不包含浏览器与 Chrome。
#        所有页面抓取一律通过 HTTP 委托给 browser-scraper 容器（见 browser_scraper_client.py）。

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PYTHONPATH=/app

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
 && echo $TZ > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖先行，利用镜像层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 业务代码与分析入口
COPY match_generator.py enhanced_analyzer.py browser_scraper_client.py ./
COPY search_tools/ ./search_tools/

# ⚠️ 刻意不 COPY config.py：
#    config.py 当前含明文 API Token（见 AGENTS.md §3.4），
#    复制进镜像会把凭据固化到镜像层中。改由 compose 以只读卷挂载注入：
#      volumes: - ../config.py:/app/config.py:ro
#    独立运行镜像时同样需要挂载：
#      docker run --rm -v "$PWD/config.py:/app/config.py:ro" ...
#    后续应改造为环境变量注入（AGENTS.md §3.4 强制项）。

# 运行期数据卷挂载点（分析结果 / 文章语料 / 比赛 JSON）
RUN mkdir -p /app/output
VOLUME ["/app/output"]

EXPOSE 8000

# 默认执行增强分析器；参数可通过 compose 的 command 覆盖
CMD ["python3", "enhanced_analyzer.py", "--resume"]
