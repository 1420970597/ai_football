# E223 Telegram 实时与补采锁顺序

更新时间：2026-11-04 14:40 UTC。状态：研究中，线上未修改。

目标：实时接收与同一消息补采编辑并发时不形成环形锁等待，保留旧版本、事务入队、租约与补采确认语义。责任目录services/api、tests、reports、docs/platform-v2；前置E222发布与新冻结窗，独立隔离PG。现有证据：E220补采503/来源消息锁阶段慢，未证明每次失败的具体根因。源码确定实时路径source→message advisory→事件写入；补采路径source→apply_claim事件FOR UPDATE→message advisory→事件写入，存在相反顺序。此前lease-after-message-wait测试正通过apply完成事件确认先于message锁，不能把该次序当必须保留业务契约。

设计候选：补采也先取得source→message锁，再apply_claim原样验证版本/owner/claim/成员/摘要、更新applied、之后原snapshot重新校验当前租约并持久化和ack；所有写入仍同一事务，任何异常回滚，不提前确认、不跳版本。删除路径的成员/相册锁范围另核对，不通过本候选声称所有相册并发已覆盖。数据库权限、锁预算、接收检查点、来源共享锁均不放宽。

验收：真实隔离PG并发屏障先让实时请求持message锁暂停持久化，发同消息有效claim补采编辑。旧顺序会持事件锁等待message，恢复实时后环等应被现有锁预算拒绝；候选必须两请求完成且最终编辑/旧版本/单当前事件/任务撤销和入队正确。不能只mock锁或用sleep声称重现；观察pg_locks确认等待关系。调整既有lease-wait测试为观察message等待后撤销租约，仍必须409且applied/ack回滚。覆盖重复claim、过期、跨消息、乱序及媒体保留原测试。先原实现负向，再冻结后定向及完整套件；失败不提高timeout。真实归因及实际改善必须后续完整窗口证明。

下一步：E220窗口终审与E222上线按原计划执行；在独立候选实现可重现负向，不在当前冻结镜像中混入本改动。

14:43负向及首修验证：首duplicate-ready样本两请求200/1项1.701秒exit0，因实时幂等路径不写事件，未触发环等，不能记作复现。改用既有pending图片同版本完成上传并发有效claim补采编辑，观察pg_locks确认advisory未授予后释放实时屏障；原实现2.483秒/1error/exit1，实际DeadlockDetectedError（message advisory与transaction锁互等），已复现。只调整锁顺序后同真实场景两请求200/1项1.710秒exit0，最终两版本、单current及最新正文正确。独立冻结e66d244还将既有租约等待用例改为pg_locks证明确实在等message后过期，保持409/claim标记回滚断言；完整Telegram API定向进行中。未宣称线上503归因、未部署。
