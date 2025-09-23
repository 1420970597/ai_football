# AI Football Web 平台

使用 Django REST Framework + SQLite 提供后端能力，Vue 3 + Vite 构建前端页面。

## 目录结构

```
web/
├─ backend/               # Django 工程
│  ├─ ai_football_site/   # 项目配置
│  ├─ matches/            # 比赛、文章、AI 配置相关模块
│  ├─ manage.py
│  └─ requirements.txt
└─ frontend/              # Vite + Vue 前端
   ├─ src/
   ├─ package.json
   └─ vite.config.js
```

## 后端运行

1. 安装依赖
   ```bash
   pip install -r web/backend/requirements.txt
   ```
2. 生成并迁移数据库
   ```bash
   cd web/backend
   python manage.py makemigrations
   python manage.py migrate
   ```
3. 启动开发服务器
   ```bash
   python manage.py runserver
   ```
   首次运行会创建球队、比赛、文章、AI 配置等数据表，并在 `SiteConfiguration` 中填充默认提示词。

### 主要 API
- `GET /api/matches/`：返回今日与明日的比赛列表，可选参数 `league`、`team`、`status`、`upcoming`
- `GET /api/matches/<id>/`：比赛详情，包含球队统计、文章、AI 汇总
- `GET /api/matches/<id>/timeline/`：AI 生成的时间线数据
- `GET/PUT /api/settings/`：查询与更新模型地址、秘钥、提示词、更新周期等
- `POST /api/updates/trigger/`：手动触发数据同步（需后端运行时的线程池）

后端启动时会初始化 Selenium 抓取线程池和数据更新协调器，用于抓取文章正文、调用大模型生成摘要及汇总结果。相关线程仅在 `runserver` 模式且主进程中自动启动。

## 前端运行

1. 安装 Node 依赖
   ```bash
   cd web/frontend
   npm install
   ```
2. 开发模式
   ```bash
   npm run dev
   ```
   Vite 默认监听 `5173` 端口，并通过代理将 `/api` 请求转发到 `http://127.0.0.1:8000`。
3. 构建与预览
   ```bash
   npm run build
   npm run preview
   ```

前端提供以下核心界面：
- **首页**：今日/明日赛程、AI 最新结论、文章数量、过滤条件
- **比赛详情**：球队走势、AI 详情、媒体文章、时间线
- **系统设置**：配置模型地址、秘钥、提示词以及同步周期，并可手动触发数据刷新

## 后续可拓展
- 增加 Celery 或 APScheduler 定时任务，替代简单的后台轮询
- 丰富比赛统计与可视化展示（ECharts 等）
- 为 API 加入认证、速率限制等生产级约束
- 编写端到端测试，确保数据抓取、分析与展示链路稳定
