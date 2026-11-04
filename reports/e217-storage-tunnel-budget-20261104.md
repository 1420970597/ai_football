# E217 存储隧道突发 CPU 预算

更新时间：2026-11-04 12:25 UTC。状态：设计已记录，未生效。

目标：减少数据库与对象存储共用 SSH 隧道的 CPU 限流长尾，不中断持久连接。责任目录 docker-compose.yml、docs/platform-v2、reports；无数据库结构或业务规则变更。依赖 E216 完整冻结窗口结束、Docker 在线资源更新及原容器身份检查。

当前证据：运行 storage-tunnel=0.1 CPU/128MiB；有效调度周期增830、限流增203（24.46%）、throttled_usec+19860093、usage_usec+2482663。平时CPU瞬时值低，不能排除每100ms仅10ms额度造成的突发阻塞；但尚未证明全部SQL超时因此引起。E215存在ready超时、来源状态短暂异常、live一次503；这些是对比基线，不将后续变化直接归因为CPU。

设计：默认TUNNEL_CPUS从0.1提高到0.25，仍保留环境显式覆盖；本机七项常驻业务CPU上限合计由1.65增加到1.80（API .4、worker .25、Hermes .55、MCP .15、web .1、remote-link .1、storage .25），增加0.15，内存、进程、网络、权限及密钥不变。采用Docker update在线修改原项目指定容器，无重建、无重启、不关闭SSH/TCP，不改其他项目。当前环境若有显式TUNNEL_CPUS，先核对项目配置来源并在项目运行引用中同步，秘密不输出。回滚仅将同一容器CPU改回0.1并同步配置，不回滚数据或令牌。

实现动作：独立候选修改Compose默认预算，Docker配置解析核验默认/显式覆盖；当前E216全窗内不修改运行资源。E216终审后记录原容器ID/StartedAt/CPU计数和当前就绪状态，在线更新指定容器并验证身份未变、资源实际生效、SELECT1/ready及S3授权读取，开始新完整1800秒观察。若负载允许，记录同长度cgroup增量、同事件API延迟与失败率；样本数量、自然消息内容和上游负载差异须明确。

验收：同容器无重启、配置可复现、六群租约不中断、自然新帖当前attempt回执齐全，资源受限且无新数据错误。CPU限流下降和延迟变化分别报告，不将瞬时改善包装为端到端三秒；如没有改善，保留测量并继续定位链路/事务，不盲目继续加CPU或关闭超时检查。

## 实际生效

2026-11-04 12:51 UTC，在E216完整1802.9秒退出0后cherry-pick为b66166f，Docker update --cpus .25 ai-football-v2-storage-tunnel-1退出0。Docker身份比对exit0：ID和StartedAt完全不变（仍2026-10-31T07:28:23.043875975Z），NanoCpus100000000→250000000、内存134217728不变。只读前后smoke均exit0：before DB四次389.1/249.5/130.6/253.0ms、S3原图6556字节/783.7ms；after510.0/255.2/257.1/128.6ms、同图同hash/909.9ms，两次ready200。不声称这组短样本证明延迟改善。

首次docker cp到只读rootfs被拒exit1，未更改容器配置；改为docker exec在现有/tmp写任务脚本。第一次脚本因/tmp入口缺PYTHONPATH报ModuleNotFoundError，exit1/169字节stderr、0样本，未计完整观察。显式PYTHONPATH=/app后第二窗12:53:39.504117 UTC启动，首样本health/ready200、六租约有效、无来源错误；完整1800秒结论待13:23:40之后。现运行API/worker/MCP/web及Hermes上下文保持E216版本，E218及E219仅候选，窗内不切换。
