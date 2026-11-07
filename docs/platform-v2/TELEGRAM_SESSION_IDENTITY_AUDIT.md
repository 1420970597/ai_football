# Telegram 授权会话接管身份校验

更新：2026-10-27。工作包：P20-01最小身份门禁。范围仅采集器身份接管、相关隔离测试和本文；不修改公共API、审核、全局任务或配置，不重启生产，不真实登录或请求Telegram。

## 设计与执行记录

- 目标：接管已经保存的授权会话前，用官方客户端get_me返回的稳定用户编号，与已有加密配置中telegram_account.id比较；昵称和用户名不作为身份凭据。
- 前置：沿用已有telegram_account，不增加平行身份来源或默认账号；显式扫码授权仍通过原远端原子持久流程建立新身份。
- 已确认缺口：connect只检查is_user_authorized，并把保存的昵称/编号直接当当前身份；未验证会话实际所属账号。
- 决策：缺预期编号、非法编号、返回身份不可核验、错号分别拒绝接管，不自动覆写配置；get_me有整体截止，暂态失败可下轮重试，确定错号等待身份/会话配置改变。验证绑定客户端、会话和预期稳定编号，普通同步复用验证结果，断线重连重新核验。
- 验收：同号、昵称变化、错号同昵称、缺/错预期编号、未授权、get_me超时/网络失败/重复密钥、重连/热配置及并发接管，连同既有历史接收、相册、管理员和普通成员检索离线回归。
- 当前状态：实现及冻结Docker专项验证通过；17项新增身份测试和关联104项测试退出0。主工作线负责最后完整回归和统一部署，本包不自行操作生产。
- 失败处理：不注册消息处理器、不读取群目录、不确认接收检查点，不保存新账号；保留旧配置，安全状态只含固定原因类别，不回显会话或账号编号。

## 已实现行为

1. 复用已有telegram_account.id，严格接受正十进制稳定编号；拒绝布尔、小数、负数、前导零、超范围、昵称和损坏记录。缺预期身份在创建连接前拒绝，既有昵称或用户名不能作为回退。
2. 授权后、安装消息处理器前调用get_me，整体五秒截止。稳定ID一致才接管；昵称或用户名变化不影响相同身份，也不自动写配置。
3. 确定错号保存仅内存的会话及预期ID绑定摘要，本轮不反复连接；身份或会话配置改变清除该拒绝闸门。缺身份、不匹配、不可核验、超时和暂时不可用分别用固定类别输出，不带实际/预期账号编号。
4. 成功验证绑定具体客户端、会话和预期稳定ID；普通同步与目录查询复用，不对每条消息增加get_me。断线重连重新核验；授权撤销仍使用原失效会话指纹闸门。昵称配置变化只更新展示，不触发重连；稳定ID变化先断开旧客户端。
5. 接管及热配置更新复用既有session_state_lock串行，避免两个控制请求创建重复客户端或身份核验期间切换配置；原二维码显式授权仍以远端保存成功为接管前置。
6. 新测试共享样本增加明确测试账号，旧“换会话后新编号”样本的get_me同时切换真实假编号，避免原测试错误地只靠保存昵称通过。未调整真实配置，也未改群组、作者、媒体或检查点契约。

固定原因：session_identity_expected_missing、session_identity_expected_invalid、session_identity_mismatch、session_identity_unverifiable、session_identity_timeout、session_identity_unavailable。拒绝状态为failed且authorized=false；目录返回unauthorized并附固定原因，不能继续枚举成员或接受消息。

## 冻结验证

首轮四模块84项，1.448秒，退出0。随后新增身份配置并发/错号接管边界，重新冻结，最终命令：

```sh
docker run --rm --cpus=.5 --memory=384m --network=none \
  --tmpfs /check:rw,size=96m,mode=1777 \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/check/services/api:/check/services:/check/tests:/check \
  -v /root/ai_football:/source:ro -w /check \
  ai-football-verifier:v2.0.0-dev sh -c \
  'cp -a /source/services /source/tests /check/ && sha256sum services/collector/telegram.py tests/test_telegram_collector.py tests/test_telegram_session_identity.py && python -m unittest tests.test_telegram_session_identity tests.test_telegram_collector tests.test_telegram_directory tests.test_telegram_media_scheduling tests.test_fault_recovery_matrix.TelegramFloodRecoveryTests tests.test_collector_lifecycle -v'
```

2026-10-27 19:27:52 UTC：**104项、1.467秒、零失败/错误/跳过，退出0**。涵盖17项新身份门禁、1005条历史续扫、持久媒体/相册、管理员及普通成员检索、FloodWait恢复和采集生命周期。容器禁网，全部Telegram客户端、HTTP及对象存储均为替身，不读取真实会话。原生控制服务测试只在该禁网容器内loopback运行。

日志：.runtime/telegram-session-identity-20261027/final-directed.log。被测源码先复制到容器内96MiB tmpfs再运行，冻结后未修改业务源码。源码摘要：

| 范围 | SHA-256 |
| --- | --- |
| Telegram采集器 | 72ec4605af426611c2cee79ac7e85481b3bed7cb82e54c0001b603f0e263400d |
| 既有采集测试 | 1413954a7bc0cadc01adf25e52c95c0145c86e64094b387a2aeba57a817fa1ed |
| 新身份测试 | 2c8c98c137bd39d278e60288afffbde4c8935a99cd648990fc7d4897cbdd8cf8 |

项目文本核对中一次工具包装语法错误未执行，一次rg括号未转义返回2，改用固定文本搜索后退出0；测试本身首轮及最终均通过。

## 验收边界

本包只证明保存会话接管不会以昵称冒充稳定账号。既有四来源生产运行未被本包重启或请求，真实接管/账号身份事实应由主工作线统一部署后只读核验。没有新增Telegram配置项、没有自动替换旧账号、没有扫码或登录，没有改全局任务、进度、记忆或Git提交。
