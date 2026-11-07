# E437 完整数据库等待观察

2026-11-06 09:41:54.172617 UTC启动，1801个1秒采样，实际1800.154秒；观察容器与汇总均退出0。只读取本项目数据库账号和明确application_name，不输出SQL、参数、账号或凭据。

采样活动4107条；6个阻塞样本，最大被阻塞查询1.066秒；活动查询最长4.268秒。16个quote_update活动样本，quote_upsert活动样本0。本窗没有捕获此前5秒upsert超时的完整同PID事件，不能据此宣称根因解决。

命令：`bash .runtime/e209-live/e437-db-wait-watch.sh`、`docker logs ai-football-e437-db-wait-watch`、Docker内`e437-summary.py`。证据为`e437-db-wait-watch.{jsonl,exit}`和`e437-db-wait-summary.json`。首汇总因证据目录属主权限失败1，改专用root只读源/仅写项目证据路径后实际0，未放宽生产权限。

下一步继续在出现真实超时时对齐PID与等待类别，避免把隔离性能改善或未复现当作生产修复验收。
