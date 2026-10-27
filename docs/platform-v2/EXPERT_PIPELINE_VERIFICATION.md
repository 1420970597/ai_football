# 专家审核、完整匹配与版本化收益验收

日期：2026-10-26。状态：后端隔离行为通过，真实上游和最终发布尚未通过。证据为可核验源码、真实 Docker 及临时 PostgreSQL 执行（一手 A 级）；用例中的球队、作者、令牌与结果均为显式测试样本，不是业务数据或实盘盈利。

## 最终行为

审核任务按消息版本入队。每次领用生成新尝试标识，90 秒租约与 20 秒心跳绑定该尝试；超时旧工作器无法覆盖接管者。失败有有限退避，最终崩溃的过期任务进入失败态。模型返回一段文字不会使任务成功，成功须有系统实际接受的推荐。

检索同时覆盖赛前及滚球。首次查询固定整个符合条件的候选快照，分页只限制每页资源，不把二十条当成总边界。服务端验证签名游标、范围及连续页，遍历结束后才允许双方球队、联赛及开赛时间的唯一消歧；歧义保留未解决。盘口读取也绑定尝试与比赛，记录精确报价和比分上下文；变价、比赛转阶段、比分变化、停盘、失效或编辑后必须重新核实。

```mermaid
sequenceDiagram
    participant T as 消息接收器
    participant D as 远端台账
    participant W as 审核工作器
    participant H as 本机Hermes
    participant M as 受限MCP
    T->>D: 原文版本、媒体状态与任务事务提交
    W->>D: 跳锁领用与尝试租约
    W->>H: 原文、回复及结构化图片
    H->>M: 完整候选分页
    M->>D: 固定快照与连续游标
    H->>M: 唯一比赛消歧与全部盘口读取
    H->>M: 提交多项精确报价标识
    M->>D: 校验租约、消息、盘口与比分后保存证据
    W->>D: 按已接受推荐决定成功或未解决
```

MCP 使用真实 SDK 流式 HTTP，服务端 Bearer 和内部 Host 白名单均生效。只有比赛检索、唯一消歧、盘口读取和推荐记录四种工具，不向模型提供通用数据库、账户或投注接口。图片逐张使用原生结构化输入，校验媒体类型及字节签名；每图 8 MiB、每次审核 24 MiB、最多十图。缺图、类型不符或超过预算失败，不静默丢图后声称审核完成。模型路由原生视觉是否真的有效仍须真实提供方验证。

## 结算和指标口径

| 项目 | 已实现规则 | 证据不足的处理 |
| --- | --- | --- |
| 独赢 | 足球三项、系统推荐赔率 | 不支持方向保留待核对 |
| 让球与大小 | 十进制；四分之一盘拆相邻半盘各半本金 | 不支持线值不判输 |
| 全场及半场 | 必须有对应期间比分；下半场由完整半场与全场推导 | 缺少半场比分不填零 |
| 滚球 | 明确全期间或余下期间；余下期间减推荐时比分基线 | 未确认盘口比分规则保持 pending |
| 取消或腰斩 | 仅上游明确作废证据可归零 | 不用比赛消失或时钟推断作废 |
| 修订 | 赛果源时间单调、内容哈希幂等、版本不可变；逐条重算最新投影 | 同时间冲突及旧版本拒绝 |

推荐时赔率、期间、方向、比分及消息身份保留。原文倍率不进入计算。当前源报价按五位小数保留，不用四位精度拒绝有效编码。赛果与计算版本保留完整历史，修订不重复累计；撤回消息仅停止新动作，保留原资金证据，专家模拟与真实订单结算分账。

准确率为 `(全赢 + 半赢×0.5) / (全赢 + 全输 + 半赢×0.5 + 半输×0.5)`，走盘、作废不入分母。净收益是每条本金一的净额合计；平均每单收益除以已结算推荐数，包含走盘和作废；有效本金 ROI 除以有效本金，半盘计 0.5、走盘作废计零。无分母显示未知，不能显示 100%。曲线按最新结算 UTC 日期聚合，明确是最新赛果投影，不冒充真实资金流。

公开比分支持上海等明确时区的比赛日、赛前、滚球、完场与取消；未知报价时间不报实时。会员按作者读取完整原文，包括失败、未解决、修订和删除标记；详情联动工具轨迹、比赛和盘口。图片由鉴权接口读取私有对象，不开放任意对象键。游客不能绕过原文和图片权限。

## 已执行验证

统一验证器固定 API、采集与 MCP 顶层依赖。运行前复制本项目到容器内临时目录，排除 Git、私有运行配置、缓存与依赖产物；冻结副本测试期间不修改。只使用本任务具名临时数据库和假身份，未写生产库、S3 或任何真实订单。

```sh
docker run --rm --cpus=0.75 --memory=512m --network=ai-football-v2-test-net \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/tmp/frozen/services/api:/tmp/frozen/services:/tmp/frozen \
  -e TEST_DATABASE_ADMIN_URL=postgresql://postgres@ai-football-v2-pgverify:5432/postgres \
  -e IDENTITY_TEST_DATABASE_URL=postgresql://postgres@ai-football-v2-pgverify:5432/postgres \
  -e TELEGRAM_TEST_DATABASE_URL=postgresql://postgres@ai-football-v2-pgverify:5432/postgres \
  -e LEYU_RENEWAL_TEST_DSN=postgresql://renewal_protocol_test:isolated-renewal-test-only@ai-football-v2-pgtest/renewal_protocol_test \
  -v /root/ai_football:/src:ro ai-football-verifier:v2.0.0-dev sh -c \
  'set -eu; python -c "import shutil; shutil.copytree(\"/src\", \"/tmp/frozen\", ignore=shutil.ignore_patterns(\".git\",\".runtime\",\"__pycache__\",\"node_modules\"))"; cd /tmp/frozen; exec python -c "import logging,unittest,sys; logging.disable(logging.CRITICAL); result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.discover(\"tests\")); sys.exit(not result.wasSuccessful())"'
```

最终 **202 项、零跳过、42.193 秒、退出码 0**。涵盖身份、配置、迁移、续期、WS、采集生命周期、Telegram、分页消歧、审核队列、图片、MCP 流式协议和结算。测试目录继续新增后数量会变化，本记录只证明本次冻结内容。

失败历史：首次统一运行160项时验证器少 collector 包路径，退出码1；补路径后188项时，新采集测试停止时序及固定四位小数字符断言分别失败，退出码1。已改为事件前置和十进制值断言。MCP 首次动态加载缺模块登记、Host 不含端口及跨任务退出 lifespan 的测试前置失败，均退出码1；修正真实 SDK 生命周期后流式测试通过。没有删除断言、放宽权限或把失败改为跳过。

## 尚不能宣称完成

真实数据采集、扫码、远端持久写入、Hermes 提供方工具及视觉回路、官方赛果确认、完整网页浏览器、Android 安装、个人账户及订单、恢复、部署和 Release 仍分别验收。旧协议只有结束通知与缓存比分，尚无通用官方结果确认接口证据，不把它们包装成 confirmed 赛果。
