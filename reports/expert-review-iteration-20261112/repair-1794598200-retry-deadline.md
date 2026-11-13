# 实时审核重试预算修复

更新时间：2026-11-13 20:27 UTC。仅处理 UTC [19:30,20:00) 已确认问题
`b88bd07f770149f06fcdb521598310bad0e4ed0dcfe63025bec968b3d676d0ba`。

原最小复现在实际运行工作器相同源码上失败：检测预算剩余约16秒，提供方暂时失败后仍安排60秒后重试。修复只改审核失败分支：从原始 detected_at 再计算剩余预算，退避大于等于剩余预算时记明确的预算终止轨迹并进入 unresolved，不缩短提供方退避、不重置检测时间、不伪称截止已经发生。保留已接受证据、尝试身份与 running 状态栅栏；历史和未知检测时间维持原策略。

代码冻结后，现有 Docker 冻结入口运行 test_review_retry_deadline、test_review_detection_deadline、test_review_worker、test_review_worker_runtime、test_review_worker_cleanup、test_review_scheduler_deadlines：118项、零失败、零跳过，36.202秒，退出0。新增9项分支回归及1项隔离 PostgreSQL 不可再次领取回归。原最小复现同命令修复后退出0，状态 unresolved、重试延迟0。生产只读 TLS1.3 查询退出0，限定窗口28版本、9个截止记录；本轮不重新分析消息，analysis_status=not_run。

原复现首次因容器默认用户不能读取私有脚本退出2，加 --user 0 后得到预期失败退出1。组合验证两次因自写 PYTHONPATH 漏 services/quote_witness 失败，改用仓库冻结入口后完整通过，未删检查。首次构建 --cpus 参数不支持退出125，改用 Docker 支持的 CPU quota/period 与512MiB限制；薄层只覆盖运行镜像的 worker.py，其余源码保持运行基础。

验收证据在本任务私有 attempt 3 的 frozen/backend-tests.json、frozen.log、frozen.exit、build.log。未导出原文、原图或详细轨迹，raw manifest 为空。本修复不改赛事/盘口/图片识别；不调用模型、不重审历史、不写信号或订单。发布须继续现有空闲与会话交接门禁，发布结果另保存在同任务私有证据。未证明准确率、推荐产生率或20秒整体目标达标。
