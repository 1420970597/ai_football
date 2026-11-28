# 用户不能开启自动投注：部署开关不一致

源码修复提交：bdb41af4658e2886b5051f943902330fad7b0dbe。用户明确要求执行修复，并确认页面提示“实际交易尚未被开放，开发能力不构成实盘授权”。

## 根因证据

2026-11-04线上只读事务确认：API `live_orders_enabled=false`、后台 `auto_betting_enabled=true`；启用的个人乐鱼账户已经healthy/ready、会话与余额存在、会员自动投注权益true。用户并非被错误识别为未校验或无会员，而是在保存本人许可时被API运行开关拒绝。

远端执行器 `EXECUTOR_LIVE_ORDERS_ENABLED=false`；协议桥更固定拒绝 `live_capable=true`，只放行false。只切换API会造成“用户能保存但执行器不执行”，只切换执行器则会被桥403拒绝。必须统一三处部署配置。切换前个人启用策略0、活动资金订单0，账户任务和续期各0；不继承采集账户或替用户填写资金授权。

## 修复

- 桥增加默认false的显式开关；开启时只接受经过executor认证的严格 `POST /internal/executor/claim?live_capable=true`。其他查询组合、collector令牌越权、错误请求方法仍拒绝，资金路径和身份白名单不扩大。
- 远端Compose将执行器和桥的实盘能力设为可配置且默认false。
- 新增本机API与远端服务的配套opt-in覆盖文件，由本次已授权运营修复显式应用。部署先桥、再执行器、最后API；保留当前镜像及完整已有覆盖链，避免回退其他已上线修复。
- 用户本人选择专家、填写每单本金/单笔/每日/敞口限额和风险确认后才能启用。会员、账户绑定版本、健康、报价有效性、敞口预留与未知回执禁止重试等业务约束保留。赛前协议仍未核验，只开放已支持的滚球路径。

默认拒绝并非通过删除检查解决。修复让默认关闭成为可显式开启的一致部署策略。

## 验证记录

先增加真实HTTP桥测试，旧版本1项0.502秒exit1，明确开关后仍403，证据before-test.log。修复后远端桥/传输27项11.967秒exit0，包括角色隔离、查询严格性、非布尔拒绝、Unix连接、限流、故障恢复和日志脱敏。

干净提交的Docker Playwright完整套件exit0，双端1440/390真实填写、保存启用及关闭通过，无效限额与会员撤销仍阻断；专家选择和本人确认继续必要。替身交易发送次数0、failures=[]。证据 `.runtime/auto-betting-release/.runtime/web-integration-1321066`。

全套后端、线上切换与真实无资金领取验收随完成记录在[独立进度](../docs/platform-v2/AUTO_BETTING_PROGRESS.md)，未完成的阶段不冒称成功。

## 最终验收

完整冻结后端1306项615.338秒exit0，失败0/错误0/跳过0；机器证据committed_source_verified=true，数据库恢复difference={}。路径 `.runtime/auto-betting-release/.runtime/test-evidence/1320784/backend-tests.json`。

2026-11-04 22:12–22:15 UTC已按序应用线上显式开关：桥和执行器实际环境true、API live_enabled=true且后台总开关true；原镜像保留，collector与审核worker未重启。桥源码SHA256 `06f022cd15ccfd218f3f995bf67a144a6da7cc81db94a17d830bde06b7128257`。API健康与就绪200，审核交接锁正常释放。远端实际执行器通过真实桥发送严格live_capable=true请求返回200/idle；短事务锁住账户并确认无订单/许可，未提交投注交易，随后显式释放。

切换后本人账户仍healthy/ready，最后成功校验22:15:20 UTC，会员权益有效；个人许可仍关闭、专家0、限额未填写，未替用户创建资金授权，资金活动订单0。部署开放已审计。**用户刷新页面后可本人填写专家和限额、勾选确认后开启；原先部署门禁冲突已解除。** 实盘成交并未作为验收，不能据此声称存在真实投注收益。
