# 本人自动投注部署开关

默认配置不开放资金执行。运营明确开放后，必须统一 API、远端协议桥、远端执行器三个配置，不能仅打开后台 `auto_betting_enabled`。

- 本机现有 Compose 全量覆盖链追加 `infra/compose.auto-betting.yml`，将 API 的 `LIVE_ORDERS_ENABLED` 设为 true。
- 远端现有 Compose 全量覆盖链追加 `infra/remote/compose.auto-betting.yml`，将 api-bridge 和 executor 的 `EXECUTOR_LIVE_ORDERS_ENABLED` 设为 true。桥必须运行支持显式开关的当前 `api_bridge.py`。
- 不替换其他已有覆盖链，不改变密钥来源、账号或设备，不把开关写入全局共享配置。

切换前只读确认：个人自动投注策略均关闭，活动资金订单和账户校验任务为零，现有镜像及覆盖链有恢复记录。先桥、再执行器、最后 API；分别以 `up --no-deps` 更新指定服务。API更新使用审核空闲交接锁，避免中断审核。不得因Compose孤儿警告清理其他容器。

上线检查：API读取live_enabled=true且后台总开关true；桥只允许经过executor认证的严格`live_capable=true`请求，默认仍拒绝，其他参数与collector越权仍拒绝；执行器配置true且可完成无资金的空闲领取。无有效个人许可时不能预留或发送订单。

用户仍须本人选择专家、填写每单本金、单笔/每日/敞口限额并明确确认。会员、当前账户健康、本人绑定版本、信号有效性、当前报价、敞口及单次提交约束继续由后端执行。赛前协议未核验，仍不开放赛前自动投注。不要使用真实下注作为部署验收。

停用先通过后台总开关阻断新交易，再停止执行器新领取并核对已发出/未知订单；未知回执不重发、不清敞口。移除本次opt-in覆盖后API/桥/执行器恢复默认false，不恢复已撤销个人许可。任何实际资金已发出的状态必须完成查单核对，不能仅用回滚镜像处理。
