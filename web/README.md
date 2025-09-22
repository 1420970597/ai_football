# AI Football Web 子项目

通过 Django + SQLite 提供后端数据接口，使用 Vue 3 + Vite 构建前端赛事情报面板。

## 目录结构

```
web/
├─ backend/            # Django 后端工程
│  ├─ ai_football_site # 主项目设置
│  ├─ matches/         # 比赛/球队/AI 数据模型与 API
│  ├─ requirements.txt # 后端依赖
│  └─ manage.py
└─ frontend/           # Vite + Vue 前端工程
   ├─ src/             # Vue 组件、路由、状态管理
   ├─ package.json
   └─ vite.config.js
```

## 后端启动流程

1. 激活虚拟环境（可复用仓库根目录下的 `venv`）：
   ```bash
   source venv/Scripts/activate  # Windows PowerShell 用 .\venv\Scripts\Activate.ps1
   ```
2. 安装依赖：
   ```bash
   pip install -r web/backend/requirements.txt
   ```
3. 生成及迁移数据库：
   ```bash
   cd web/backend
   python manage.py makemigrations
   python manage.py migrate
   ```
4. 启动开发服务器：
   ```bash
   python manage.py runserver
   ```

### API 路径速览
- `GET /api/matches/`：赛程列表，支持参数 `league`、`team`、`date_from`、`date_to`、`status`、`upcoming`
- `GET /api/matches/<id>/`：单场比赛详情（含球队统计、文章前瞻、AI 分析、综合建议）
- `GET /api/matches/<id>/timeline/`：AI 分析更新轨迹
- `GET /api/teams/`：球队基础信息与赛季统计
- `GET /api/articles/`、`GET /api/ai-insights/`、`GET /api/recommendations/`：支持以 `match` 查询

## 前端启动流程

1. 安装依赖（需 Node.js 18+）：
   ```bash
   cd web/frontend
   npm install
   ```
2. 开发模式运行：
   ```bash
   npm run dev
   ```
   - 默认端口 `5173`，已通过 `vite.config.js` 代理 `/api` 到 `http://127.0.0.1:8000`

3. 构建产物：
   ```bash
   npm run build
   npm run preview
   ```

## 下一步建议
- 接入定时任务同步真实比赛/球队数据，并触发 AI 分析写入 `matches` 模型
- 在 `matches` 应用中补充权限、缓存策略，以及导出 CSV/Excel 的管理命令
- 前端可增加可视化（ECharts）以及收藏、分享等互动功能
- 配置单元测试与 API 文档（如 drf-spectacular）保障接口迭代质量

