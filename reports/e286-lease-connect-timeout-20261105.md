# E286：租约刷新连接超时定位

更新：2026-11-05 05:27 UTC。状态：研究中，生产未修改。目标定位 E285 短暂单群降级，责任 services/collector、infra/remote；前置为完整监控窗口中六来源同时运行、已取得真实故障时间。验收必须有超时所属阶段和复现依据，不能用调大超时或盲重试替代修复。

采集器只读日志（05:18—05:22）确认 05:19:20.149786 的租约刷新为 ConnectTimeout，耗时 3468.3ms；同窗口消息事件入库仍有成功 200。监控 05:19:55/05:20:28 单群 lease_deferred，05:21:01 恢复，六条有效租约及 TG 主连接没有中断。日志脱敏只保留时刻、操作、状态、类别和耗时，不输出账号、令牌或消息内容。命令 `docker exec -i ai-football-v2-storage-tunnel-1 python - < .runtime/e209-live/e286-collector-timeouts.py`，actual exit 0。

重复读取本项目远端桥网络命名空间累计 ListenOverflows、ListenDrops、TCPReqQFullDoCookies、TCPReqQFullDrop 均为 0（e286-bridge-listen-read.json，原只读检查 actual exit 0），仍不支持监听队列溢出的假设。

在远端采集器中用现有授权只读 GET /telegram/sources，4 次、间隔 2 秒、每次截止 8 秒、连接截止 3 秒，无写入或重启。actual exit 0，全部 200、HTTP/1.0，总耗时 1555.923/1170.676/1026.777/805.231ms。第一请求连接 trace 开始于 395.226ms、完成于 598.258ms；其余三次 TCP 连接耗时约 3.072/2.260/4.504ms，主要时间位于收到响应头之前。这是独立探测，不代表已有连接和真实并发条件，也不能证明之前的连接超时根因。原始 e286-connection-probe.json 保留。

当前证据不足以将 HTTP/1.0、并发限制或 CPU 配额判为根因，不改协议、扩大并发、延长租约或吞掉降级。下一步对照完整 E285 同截止桥审计，继续观测新的自然事件与故障，保持原失败记录及已入库事件证据。三秒端到端目标依然未通过。
