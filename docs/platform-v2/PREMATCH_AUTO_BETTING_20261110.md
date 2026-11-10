# 未开赛自动投注协议与状态链路

更新时间：2026-11-10 14:43 UTC。状态：代码已写、隔离验证通过、真实只读协议联调通过、本人确认桌面/手机浏览器验证通过；尚未发布或进行真实资金验收。此包由用户明确要求使用子代理执行，主线仍为专家发言抽取准确率；不修改模型网关、用户策略或生产数据，不执行真实资金测试。

目标：本人已经开启自动投注且具备有效专家/金额/限额许可时，新收到的未开赛推荐可进入同一独立订单执行链路。责任目录为 services/executor、services/api 的既有订单与派发模块、独立行为测试。前置依赖为乐鱼赛前盘口预检、限额及单关请求参数的实际历史协议或 App 静态证据，不能只删除门禁。

当前证据：现执行器在 preflight 直接拒绝非 live，三个请求均硬编码 matchType=2，预检仅接受 matchStatus=1。API eligible_signal 只接受 football/live，reserve_signal 快照硬编码 sport_id=1、phase=live；需要逐层复核派发候选及最终请求授权。旧 ACCOUNT_PROTOCOL 明确赛前交易未核验，不能当作已支持的证据。

缺口：赛前 matchType 与 market matchStatus、阶段切换时拒绝行为及 API 最终授权中的 phase 一致性。先复用既有 App/H5 和历史分析记录；不足时使用 REA 原 App 静态分析。静态参数不冒充真实交易接受性。

设计决策：仅在核验协议后扩展 scheduled，保留取消/延期/完场/未知拒绝；以订单快照和当前比赛阶段一致性阻止赛前订单跨阶段变成滚球。保留本人开关、会员、账户版本/身份、推荐新鲜度、金额/敞口预留、盘口精确线与方向、未知结果不重试。篮球共享运动范围由篮球子代理核验后提供，执行器共用文件由本包单独写入，避免并发编辑。

实现动作：定位协议证据，写明来源及局限；冻结失败测试；修订现有阶段参数、候选/预留/发送复核；冻结代码进行 Docker 协议与临时数据库行为测试。禁止新建平行交易路径或真实下注证明功能。

验收场景：足球赛前与滚球各自使用正确请求参数；停盘、阶段变化、取消、错误身份/线/方向、限额不足拒绝；赛前正常新信号可派发、预留和颁发一次许可；关闭本人开关、过期许可、未知回执、跨账户与重复提交仍拒绝；无历史补投及生产写入。失败保留原退出码并修复后重跑，不放宽检查。

初始研究实际命令：宿主只读 rg/sed/Git 检查，退出 0；错误定位文件名的只读 rg/sed 退出 2 保留（模块名称核对后继续）。此初始阶段未运行模型、App、登录或资金请求，随后协议和验证见下文。

## 协议与实现证据

REA 技能：`/root/.agents/skills/reverse-engineer-anything/SKILL.md`。复用已经验证的九 DEX 和固定 REA 6.7.0 / JADX 0.7.1、单进程 `.35 CPU / 768MiB`、网络关闭的现有分析容器，不运行 APK。类分片 `leyu-analysis-classes7.apk` SHA-256 为 `029070e8ffe05c8cf1592a945166234d81cf3c13b7d9ee3062fa3fa411c4f145`。下列均为精确方法、overload-index=0（另有注明除外），命令与完整 JSON 保存于 `.runtime/e209-live/e330-prematch-*.json`，原生退出均 0。

- `OBBetViewModel.m3`：`ev_a80a2ff89961b2a19f9d5fafcd6ec92e3dbc291ec5a5160fcee5d3836bea1148`，下注 detail 的 `matchType` 来自 `getOriginMatchType`、`sportId` 来自 `getEventSportId`，不是固定足球/滚球。
- `OBBetModel.getOriginMatchType`：`ev_39f78afe874353f33f7b5fc03db7ddbb1f6720a0d403123876a7656162aa5e64`，普通单关读取原始盘口的 `matchType`；overload-index=1 的 `ev_a3c88b88491d7b2907b5ac1cf689c9374808a82ccae0330a95e623179554ff07` 另处理冠军/虚拟/电竞，未开放这些类别。
- `OBHandicapModel.loadData`：`ev_e122c153e71e93c5a8f0cad7ae3cb83b3018ba7d41ef3fb4928c7082a015edfb`，原生盘口把 `getHmt()` 传给选项 `matchType`。
- `OBPlayModel.getMatchType`：`ev_c6361cbd027fef9871a38c2d4a5e57423029c021e67a5c43ddf0854ed01b4281`，源值 0→订单类型 2、1→1、其他→3。不能仅凭这一映射断言真实赛前可用，所以下方追加本人会话只读验证。

2026-11-10 14:33:44 UTC：只读数据库连接启用 `default_transaction_read_only=on`、4.5 秒 statement timeout；只接受唯一已有健康、本人开启且许可绑定匹配的用户账户，解密仅该账户现有会话，不使用全局采集身份。选取现有未开赛足球全场大小原生盘口（源时间 `14:33:17 UTC`），执行器现有报价/限额端点各一次，`matchType=1`。两次均 HTTP 200 / `0000000`、唯一精确市场与选项限额，`matchStatus=0,matchOver=0,matchHandicapStatus=0,status=0`，限额 min=2 / max=7500。没有登录、续期、余额修改、订单预留、交易提交或数据库写入。只读结果及散列参数见 `.runtime/prematch-20261110/native-readonly-egress.json`。

首次在 API 容器内探针因其私有网络产生 ConnectError，没有上游 HTTP 请求；独立探针使用现有 egress 与 storage 网络读取，不改生产网络配置。一次 bridge/user-defined 组合运行原生退出 125，一次新网段重叠创建退出 1，均未产生运行探针或修改已存在网络。成功只读探针退出 0；专用最小数据库 URL/解密 key 配置以 0600 暂存，探针结束立即删除，秘密值不回显、不入 Git。没有因网络失败重试登录或切身份。

已实现同一订单链路：候选筛选允许 scheduled/live 且推荐原阶段与当前赛事一致；快照使用实际 phase/sport，赛前不带滚球基准分；原生预检、限额与最终 detail 统一类型映射，市场必须与阶段状态匹配；发送授权重新核对快照的阶段/运动及请求中的类型/运动，不允许赛前跨阶段变滚球。篮球由独立子包的已核验市场白名单限制全场 37 胜负、38 大小、39 让分，篮球不接受平局选项，其他期间继续拒绝。本人开关、限额、专家范围、会员、账户版本与身份、现有五分钟原文年龄校验、未知回执保留敞口及不重发均保留。历史“10 秒内”属于尚未实现/验收的目标，用户现要求暂停效率优化，本包未改 TTL，也不声称达标。

## 冻结测试

测试镜像固定为 `sha256:a8fc0cb5dbebed71df5688f33c3963a076151c7d41ca8e7625520b8ab7ffc234`，源码基线 `4b8a0b34547669f14fe15f7e8d8f7a4084c284af` 加本包未提交改动。每次测试先复制 services/tests 到不可写挂载，运行中没有修改被测文件。隔离测试使用已完成 E3056 的具名 `ai-football-e3056-full-postgres` / `ai-football-e3056-full-private`，tmpfs 数据目录没有持久测试卷；专用 `account_state_test` CREATEDB 角色，每例创建唯一 `account_state_<uuid>_test` 再删除。guard 精确限制 hostname、username、数据库 `/postgres`，没有生产网络或连接，未修改 E3056 原库或配置。

红回归：`.runtime/prematch-20261110/red`（阶段实现前冻结），`python -m unittest -v test_prematch_execution`，11 项，8 failures / 3 errors，11.230 秒，原生退出 1。成功场景均为本例新鲜来源，不使用生产旧推荐。完整日志 `red.log`。

绿回归实际命令：

```sh
docker run --rm --name ai-football-prematch-green --user 0:0 \
  --network ai-football-e3056-full-private --cpus .25 --memory 256m --pids-limit 96 \
  --read-only --tmpfs /tmp:rw,size=32m \
  -v /root/ai_football/.runtime/prematch-20261110/green:/source:ro \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/source:/source/services:/source/services/api:/source/tests \
  -e ACCOUNT_TEST_DATABASE_URL=postgresql://account_state_test@ai-football-v2-pgtest/postgres \
  --entrypoint python sha256:a8fc0cb5dbebed71df5688f33c3963a076151c7d41ca8e7625520b8ab7ffc234 \
  -m unittest -v test_prematch_execution test_account_protocol test_account_state \
  test_order_stage_evidence test_model_recovery_order_age test_basketball_execution
```

结果：103 项全部通过，189.309 秒，原生退出 0，完整日志 `green.log`。覆盖新鲜赛前派发/预留/最终许可、足球/篮球请求类型和运动、错运动/类型/期间/方向/线/限额拒绝、原始五分钟来源不洗时间、比赛开赛或取消后禁止发送、本人关闭、跨账户拒绝、相同许可一次、未知回执不重发、原有账户续期/版本隔离/阶段证据和敞口保留。无测试资金请求发往真实上游。

最终回归：在上述 green 完整冻结副本上只替换新增的专项测试，新增能力声明与“不支持的31个篮球市场不能占满dispatch LIMIT”断言，再执行同一命令（挂载改为 `final`、容器名改为 `ai-football-prematch-final`）。104 项全部通过，115.522 秒，原生退出 0，完整 `final.log` SHA-256 为 `153d551ccb9894e61dc281a7ac7be2faece1299162f82ccdc0fa4e7d6de2b305`。测试后专用角色 `account_state_%_test` 库剩余数为 0，检查退出 0；没有清理其他库/容器/网络。

篮球真实参数追加核验：14:40:45 UTC，同一本人已存在的健康会话，严格选取当前 `scheduled/basketball/full_time/38 total`（原生线157.5，源时间14:40:04），`sportId=2,matchType=1` 的报价与限额均 HTTP 200 / `0000000`，唯一精确市场状态四项全 0，唯一选项限额 min=2/max=2500。只读结果见 `native-basketball-readonly.json`；仍无登录、续期、下注和生产写入。两体育总共成功读取4个无资金接口；第二次临时秘密配置也已删除，存在性检查退出0。

## 本人确认浏览器验证与交付

使用 Playwright 技能，固定浏览器镜像 `sha256:ece536b0cb81dd6dabfa43c57163750589f023219a28e7fc41e4b64e8b989a7c`，npx 容器内检查存在、退出0。冻结 web 和浏览器脚本到 `.runtime/prematch-20261110/browser/source`，合成接口由本地 route 拦截、network none；没有生产账户保存或真实执行器连接。更新后分别人工查看1440及390截图，文字完整换行，无裁切或横向溢出，再复制两图及manifest到仓库 `tests/browser/profitable-expert-baselines/`。两图SHA分别为 `98a3c3741e61da657ededb6667890b8de745e060f9936fd33eb6023662587c71`、`d760b8423a66f4260676223e03d74333d2f474f1db35fc737e9b16f00ca64afc`。

最终只读回归实际命令：

```sh
docker run --rm --name ai-football-prematch-browser-verify \
  --cpus .25 --memory 384m --shm-size 96m --pids-limit 256 --network none \
  -v /root/ai_football/.runtime/prematch-20261110/browser/source:/work:ro \
  -v /root/ai_football/.runtime/prematch-20261110/browser/verify:/evidence -w /work \
  sha256:ece536b0cb81dd6dabfa43c57163750589f023219a28e7fc41e4b64e8b989a7c \
  tests/browser/profitable-experts.mjs
```

结果退出0：1440/390像素对照均通过，JS错误0、无横向溢出；加载/切模式不自行授权，保存需要本人勾选，盈利模式保存保留既有手动专家与全部本金/限额，切回手动可编辑。脚本与新基线均已写回仓库，既有 `visual-check.sh` 会复制这些文件并在CI消费；不是仅更新.runtime。结果 `browser/verify/profitable-browser-results.json` SHA-256 为 `f2c1a9f4d1bc62a9f4c3ae42d19a34511d99408515262cfafea5d39419aa859a`。

正式去秘密证据清单：[`evidence/prematch-verification-20261110.json`](evidence/prematch-verification-20261110.json) 含13个变更代码/测试/基线文件的SHA-256、冻结源一致性、104项测试、两体育只读响应及浏览器结果摘要；生成时逐字确认工作树与被测冻结副本一致。`git diff --check` 退出0。共享 `execution_scope` 与 `basketball_scores` 已同篮球子包协调，API、collector、executor 的对应Docker COPY均已写好；没有新增依赖/迁移。

剩余：由主代理整合三个包后统一冻结、镜像构建、完整集成回归与发布；本子包未提交、未推送、未部署，未补投历史推荐。真实交易不作为开发测试，10秒效率目标仍未验收，不能称生产功能已发布。
