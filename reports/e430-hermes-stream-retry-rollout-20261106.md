# E430 Hermes 流式重连预算发布证据

更新时间：2026-11-06 09:29 UTC。状态：隔离验证、配置发布及实载通过；部署后完整监控进行中，三秒目标未达。

- 源码：fd7c8f67930c609f39212198b1049ddfc0f18032，远端任务分支一致。固定 Hermes v0.22.5 镜像不变。
- Docker定向52项3.488秒退出0；固定框架完整协议联调02退出0，含真实框架视觉输入、13连续页260候选、越权、取消、静默、503及恢复。首轮静默计数错误退出1保留，按固定框架一次主客户端恢复契约纠正后原场景重跑；不改框架源码或prompt。
- 干净完整Docker回归1545项751.321秒退出0，无失败/错误/跳过。机器报告754.053秒，SHA256 5de62434ebe66d4dbb0cc0519979d1507b0731c5a574c30fd8ba6cd494bf4ea5，目录.runtime/e430-candidate/.runtime/test-evidence/1789598。
- 发布证据首检查按预估1543拒绝；只读测试发现确认新增两个Compose测试亦被代理边界模块导入发现，因此清单为1545。修正私有清单后检查退出0，未跳过、删除或禁用测试。初失败与完整发现结果保留。
- 09:22:08.212559 UTC暂停新领取，09:22:09.263688自然idle后仅重建Hermes；apply/gate/controlled-rollout-02均退出0。其它六容器ID保持，镜像/挂载/权限/预算不变，仅新增HERMES_STREAM_RETRIES=0。custom/deepseek-v4.1-flash、30秒request/stale、api_max_retries=1、auto_recovery_cycles=0、native图片与预载技能实载通过。
- 发布前后434信号、637已有结算台账及不可变报价快照保持，验证退出0，未回滚数据库。
- 五条E424误收模板补审均已完成；四条聊天回应无独立推荐意图，一条三项推荐恢复两项：悉尼FC对中央海岸水手全场大2.75系统价1.67；PSIM日惹对德瓦FC全场大2.75系统价2.09。第三项堪培拉克罗地亚单队大0.75因team_total尚不支持未录入。
- 恢复结果真实浏览器1440/390各一次点击退出0，两卡、系统价格、当前attempt双回执、第三项未录入说明、关闭及无溢出/JS错误均核验；不注入响应、不写订单。证据.runtime/e209-live/e430-recovered-browser-output。
- E431原完整窗持续；E433部署后新1800秒窗及自动终审已启动。当前仍须处理译名召回、单队玩法、上游长等待及数据库偶发超时，不能宣称全部功能正常。

实际命令：e430-full-suite.sh、e430-evidence-check.py、e430-controlled-rollout.sh（02）、e430-apply.sh、e430-loaded-check.py、e430-verify-rollout.py、e430-verify-ledger.py；全部通过Docker执行，主机仅Git/Docker/文本管理。初发布清单退出1、修正后各阶段退出0分别保留。
