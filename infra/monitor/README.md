# 专家审核半小时迭代子代理

本组件只负责调度一次独立Codex子代理、单实例与结果水位；不在控制器中复制业务检索、解析、订单或发布逻辑。用户已授权每30分钟复核未解决专家发言和发现真实漏配后立即修复，准确率优先，检测到推荐20秒为独立验收目标。

首完整未来窗口为UTC `[2026-11-12T13:00:00Z, 2026-11-12T13:30:00Z)`；12:30–13:00由主代理人工处理。窗口结束后才派分析，不是把每条业务消息等30分钟再审核。

## 配置接口

Compose只使用明确的非秘密路径环境变量；auth/config文件由主代理在项目私有`.runtime`目录准备，不能写进Git或镜像：

| 参数 | 含义 |
| --- | --- |
| `EXPERT_MONITOR_IMAGE` | 本次冻结控制器精确镜像ID，`pull_policy=never` |
| `EXPERT_PROJECT_ROOT` | 原项目根绝对路径，同路径挂载以保留worktree Git指针 |
| `EXPERT_SOURCE_ROOT` | 权威业务worktree绝对路径，当前为`.runtime/refactor-wt/web-ux` |
| `EXPERT_ITERATION_STATE_DIR` | 项目`.runtime`内独占私有状态目录，控制器设700 |
| `EXPERT_ITERATION_FIRST_START` | 首窗UTC起点，默认2026-11-12T13:00:00Z，必须整半小时 |
| `EXPERT_CODEX_NATIVE` | 已安装的原生Codex二进制，非Node入口 |
| `EXPERT_CODEX_CODE_MODE_HOST` | 同一vendor/bin内现有`codex-code-mode-host`，只读挂到`/usr/local/bin/` |
| `EXPERT_COMPOSE_PLUGIN` | 已安装Compose插件，默认`/usr/libexec/docker/cli-plugins/docker-compose` |
| `EXPERT_AGENT_AUTH_FILE` | 项目受保护认证文件，只读挂入，不复制/输出内容 |
| `EXPERT_AGENT_CONFIG_FILE` | 最小model/provider配置，只读挂入；不带全局MCP、钩子或历史 |

控制器还接受`EXPERT_ITERATION_TIMEOUT_SECONDS`（1–1800，默认1800）、`EXPERT_ITERATION_RETRY_SECONDS`（1–1800，默认300）、`EXPERT_ITERATION_SCHEMA`及`EXPERT_CODEX_BINARY`。Compose固定30分钟最大运行和5分钟失败退避，不允许多代理同时写同树。

`--once`至多处理一个已闭合窗口；尚未到期时只写等待状态并退出0。`--dry-run`只准备到期窗prompt和metadata，不运行Codex、不推进水位。烟测应使用单独状态目录及明确已经闭合的窗口，不能污染持续任务水位。

## 运行与隔离

Dockerfile复用现有精确Hermes工具镜像`sha256:9c5561579f3ffdf333401797094ac3a743738d6c25d40f4331c7bf77b694238d`作为运行时（API镜像缺Git/SSH，不能承担修复和冻结），复制两个控制器文件及完整依赖快照，无安装、数据库迁移或应用启动。该基础镜像现成Git 2.47.3、OpenSSH 10.0p2、Docker CLI 26.1.5；包的精确Debian版本见`toolchain-packages.tsv`，Python依赖见`toolchain-python-packages.json`，不声称仅顶层版本即可复现。Compose插件和Codex从本机现有工具只读挂载，不修改全局。主代理冻结代码后顺序构建并以精确镜像ID启动；本工作包实现阶段不启动服务、不调用模型。构建上下文仅本目录：

```sh
DOCKER_BUILDKIT=0 docker build --cpu-period 100000 --cpu-quota 40000 --memory 384m \
  -t ai-football-expert-monitor:20261112.1 infra/monitor
```

现有原生二进制路径：

```text
/usr/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex
```

子代理命令为`codex -a never -s danger-full-access -C <权威树> exec --ephemeral --json --output-schema <schema> -o <临时final> -`，prompt由stdin传入，每窗独立，不用resume/--last、不读全局会话。`CODEX_HOME`是128MiB临时挂载；auth/config各自只读嵌入；不挂全局`~/.codex`或873MiB历史库。`-o`指向128MiB `/tmp` tmpfs，控制器读取结构化结果后仅将计数、已验证时间/状态、字节数和SHA持久化，自由说明及路径不复制；临时文件随本轮退出清除。

主代理真实CLI烟测发现code-mode-host缺失及workspace-write的bwrap不可用；第三次以同vendor目录的code-mode-host、`never + danger-full-access`在专用Docker中成功执行权威树pwd与API存活查询。因此使用Docker作为明确受信任运维边界，不新增privileged、CAP_SYS_ADMIN或host网络，也不安装宿主工具；这不是权限受限的Codex沙箱。模型可执行范围仍依赖项目约束、明确mount及审计，不应宣传为强隔离。真实完整窗口业务分析仍需另验收，CLI烟测不代表持续任务已生效。

Docker预算0.5CPU/768MiB/192进程，日志轮转2MiB×3。Docker socket给予宿主控制能力，属于受授权项目运维信任边界，不是强安全沙箱；必须以AGENTS及任务prompt限制项目范围，不能宣传为技术隔离。项目根可写挂载用于Git/worktree、既有配置引用及冻结验证；子代理唯一业务写目标仍是指定权威树。子代理默认只读分析，确认缺陷后才按原项目冻结/测试/发布门禁最小修复，不改变财务、账户、源配置、资金许可、模型提供方或其他服务。

已授权Git推进可显式叠加`-f infra/monitor/compose.git.yml`，并设置非秘密路径`EXPERT_GIT_KEY_FILE`、`EXPERT_GIT_KNOWN_HOSTS_FILE`，分别只读挂为`/run/expert-git/key`与`/run/expert-git/known_hosts`。仅复用用户本次明确授权的现有文件；不挂整个SSH目录或全局Git配置，不关闭主机校验，不使用身份代理。`GIT_SSH_COMMAND`固定`IdentitiesOnly=yes`、`BatchMode=yes`、`StrictHostKeyChecking=yes`及指定known_hosts。作者和提交者统一`Project Automation <automation@localhost>`，不使用用户姓名。基础Compose无SSH挂载；未启用或权限不可用时报告阻塞。

## 水位与失败处理

- `.runtime/.../supervisor.lock`：非阻塞flock，第二实例退出3。
- `progress.json`：初始`finished_watermark=first_start`；只在下一连续窗子代理退出0且结构化结果`status=complete`后推进。
- `windows/<start_epoch>.json`和`history/<start_epoch>-<attempt>.json`：窗口start/end、状态、attempt、起止时间、退出码、超时及错误类别。metadata不含原文、个人ID或模型自由文字。
- `attempts/<start_epoch>/<attempt>/`：权限700，controller prompt及脱敏events/final/output-summary，禁止提交运行文件。JSONL每行仅白名单type/item_type/id/status/exit_code、原字节数与SHA；未知类型/ID不复制。stdout按流解析、单事件内存上限1MiB，超大/无效事件仅记统计；stderr只记字节数、SHA及枚举失败类别，不生成原始stderr文件。完整command、工具stdout、reasoning、消息和模型自由说明不落盘。
- 失败、blocked、结果缺失/窗口不符或1800秒超时均不推进；保留原失败记录，默认5分钟后重试同窗。后续窗口不会悄悄丢失，也不会与前一轮并发写入。
- 控制器重启从水位补窗；若成功记录已落盘但水位尚未提交，恢复该连续成功水位，不重复子代理。异常退出中的旧running会在新attempt中保留previous_state并重新分析，不能把旧输出当成功。
- 子进程独立进程组，超时/停止先TERM、10秒后KILL；不清理或终止其他Docker服务。失败状态使健康检查失败，容器存活不冒充分析健康。

脱敏controller日志另按窗口起点保留72小时，不以修改时间续期；失败metadata/水位长期保留。业务原文、图片引用及详细轨迹只允许代理写`attempts/<start>/<attempt>/raw/`，每文件须在写入前登记`raw-evidence-manifest.json`，格式`{"schema_version":1,"files":[{"path":"raw/opaque.json","posted_at":"2026-11-12T13:00:00Z"}]}`。混合消息/回复/图片使用最早原始posted_at，不得用导出/窗口/编辑时间续期；满72小时的原文不得读取或导出。控制器每次调度检查manifest，等待周期不超过30秒，子代理运行中每15秒检查，按posted_at删已过期注册文件；仅处理本轮raw内非符号链接文件，不递归删除陌生内容。无效manifest留下失败类别并阻止本窗成功水位。该约束不把代理违反manifest约定的任意文件变为可安全清理对象，违规仍须报告修复。公开报告只含脱敏计数、证据定位、提交和限制。

若Docker父进程被强杀，进程命名空间销毁将终止子代理；已经独立创建的Docker测试容器仍按各测试既有trap及专属命名回收，不用全局清理。

## 最小验证

新测试覆盖窗口/水位/失败/锁、posted_at保留及越界、脱敏流式输出、有界内存、真实无模型双pipe与子代理命令，不调用模型或生产业务。应用测试全部在Docker中运行：

```sh
docker run --rm --network none --cpus .25 --memory 256m --pids-limit 64 \
  -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD:/source:ro" -w /source \
  --entrypoint python sha256:a6debc6b0401b2ffbcc2e8ec6283d52f1e89b5eb51438eb2f93a30b82ef79616 \
  -m unittest tests.test_expert_iteration_supervisor
```

部署前另验证Compose渲染、精确镜像、只读认证、真实独立子代理JSON结果及退出码；首完整窗口结束后检查报告/水位/超时和真实样本，才可称持续子代理已生效。模型外部失败、没有样本、20秒超标都必须如实报告。

## 首次候选冻结验证记录（已被日志隐私修复替代）

2026-11-12 13:15 UTC：状态为隔离验证通过，真实子代理、持续调度与发布验收尚未运行。构建按上方`.4CPU/384MiB`命令完成，退出0，最终镜像为`sha256:3c1e1a8204be8e88a4eb0242e7cf59318459a5d0e4ab4e6d5b9bf04edd35ebbd`，标签`ai-football-expert-monitor:20261112.1`。控制器、结果schema及两份依赖快照逐项比较镜像和冻结源码SHA，四项全部一致；控制器`--help`退出0。

上方Docker单元命令以`-v`运行共19项，0.135秒，退出0。Compose用非秘密路径环境变量执行`docker compose -f infra/monitor/compose.yml config --quiet`退出0，`git diff --check -- infra/monitor tests/test_expert_iteration_supervisor.py`退出0。以最终镜像、`--network none --cpus .25 --memory 384m --read-only`及32MiB临时目录，只读挂现有Codex和Compose插件后执行`git --version && ssh -V && docker --version && docker compose version && codex exec --help`退出0；仅验证工具入口，未调用模型、未连接生产数据库、未启动长期容器。

源码SHA256为`ebe42c67f18dd8d41b7fe676d586414297c0f8ca48a59f4e0539afafd8820574`，schema为`569418de92a306dd08efe75ef645bbdcba875fbe9a3156638ae70b342a38ead7`，Debian快照为`839789347c75ff6957b210e7ecf92030672d1b207452702066210f070a6f4e4b`，Python快照为`84602771a69520440cfca678c748fa2161ba6ecee22d2157429f923f6621e88e`。下一步由主代理注入项目最小认证/provider配置，用独立状态目录完成真实闭合窗口烟测，核验Docker命令沙箱可用与结构化结果，再串行启动持续容器。

## 日志隐私修复后的最终冻结

2026-11-12 13:29 UTC：新控制器源码SHA256为`2f577976f79149cfb89a37b99c209fd92902f4387f98b6116122624f93bf5784`。同上构建命令仅将tag改为`ai-football-expert-monitor:20261112.2`，限制`.4CPU/384MiB`，退出0，最终镜像`sha256:7c63286d441335da47509886c40fdde924878ef8b85d53631cfde6a7efa6b7d1`；应使用该镜像，首次`.1`候选已被替代。镜像内控制器/schema/两份依赖快照与冻结源码SHA逐项比较全部一致。

新增六项隐私/TTL行为测试后，API测试镜像运行25项0.513秒退出0；最终控制器成品镜像以`--network none --cpus .25 --memory 256m --read-only --tmpfs /tmp:rw,nosuid,nodev,size=32m`运行同25项0.195秒退出0。包含真实无模型子进程双pipe输出脱敏、1MiB事件内存边界、最终自由文本去除、旧回复按posted_at在新窗过期和恶意manifest不得越界。基础及可选Git overlay共同执行`docker compose ... config --quiet`退出0，`git diff --check`退出0。未执行模型、生产数据库、长期容器或提交操作；真实CLI smoke由主代理串行完成，完整窗口分析和持续任务验收仍归下一阶段。
