# 专家审核半小时迭代子代理

2026-11-14持续验收的只读语义审计改用独立Hermes容器：

- 配置为同目录compose.semantic-audit.yml；仅既有模型认证的私有env和只读配置，禁止传入MCP令牌、业务库连接或实时会话目录。
- 审计器EXPERT_AUDIT_HERMES_CONTAINER指向ai-football-tg-semantic-hermes；配置成实时ai-football-v2-hermes-1直接失败，不能共用实时0.55CPU配额。
- 同版本镜像，独立0.30CPU/768MiB/96进程上限、只读根、无能力、无提权，目录为tmpfs；只接egress网络。审计AIAgent仍禁用全部工具、持久会话、记忆与后台review。
- 监控控制器重启仅重试尚未落盘的只读审核；已有逐条结果、快照哈希、失败窗口与通过标准不变。该资源变更后从下一个完整半小时重新计连续次数。
- 语音未转录、图片不可读、证据不全必须保持uncertain，不得靠排除消息取得三窗通过。

本组件调度独立Codex扫描与修复子代理，始终单实例串行；不在控制器中复制业务检索、解析、订单或发布逻辑。用户已授权每30分钟复核未解决专家发言和发现真实漏配后立即修复，准确率优先；业务检测到推荐20秒是独立观测目标，不能把准确识别所需证据删掉来伪造达标。

2026-11-13事故修复：旧版把分析完成与修复/发布成功绑定，13:00–13:30窗口53次终态失败后第54次被停止，后续窗口没有覆盖。v3将枚举游标、完整分析coverage和repair backlog分开。失败/partial是明确覆盖缺口，永不阻塞后窗；不能把已记录缺口包装成完整成功。原Git、windows/history/attempts完整保留。

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

控制器接受`EXPERT_ITERATION_TIMEOUT_SECONDS`（默认600，扫描有效上限600；兼容旧1800配置但压至600）、`EXPERT_REPAIR_TIMEOUT_SECONDS`（60–900，默认600）、`EXPERT_ITERATION_RETRY_SECONDS`（1–1800，默认300）、`EXPERT_ITERATION_SCHEMA`及`EXPERT_CODEX_BINARY`。失败补查每窗最多3次，总次数不因重启清零；冷却300/600秒，耗尽显式exhausted。不允许多代理同时写同树。

`--once`至多执行一个调度任务（扫描或修复）；无可执行任务时等待状态退出0。`--dry-run`只准备prompt/metadata，不运行Codex、不增加调度尝试次数或覆盖水位。`--health`只检查实际覆盖、心跳和当前任务截止，不取得写锁。烟测必须使用独立状态目录，不能污染持续任务账本。所有真实运行/导入由主代理统一部署阶段执行。

## 运行与隔离

Dockerfile复用现有精确Hermes工具镜像`sha256:9c5561579f3ffdf333401797094ac3a743738d6c25d40f4331c7bf77b694238d`作为运行时（API镜像缺Git/SSH，不能承担修复和冻结），复制控制器、纯调度模块、结果schema及完整依赖快照，无安装、数据库迁移或应用启动。该基础镜像现成Git 2.47.3、OpenSSH 10.0p2、Docker CLI 26.1.5；包的精确Debian版本见`toolchain-packages.tsv`，Python依赖见`toolchain-python-packages.json`，不声称仅顶层版本即可复现。Compose插件和Codex从本机现有工具只读挂载，不修改全局。主代理冻结代码后顺序构建并以精确镜像ID启动；本工作包实现阶段不启动服务、不调用模型。构建上下文仅本目录：

```sh
DOCKER_BUILDKIT=0 docker build --cpu-period 100000 --cpu-quota 30000 --memory 256m \
  -t ai-football-expert-monitor:20261113.1 infra/monitor
```

现有原生二进制路径：

```text
/usr/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex
```

子代理命令为`codex -a never -s danger-full-access -C <权威树> exec --ephemeral --json --output-schema <schema> -o <临时final> -`，prompt由stdin传入，每窗独立，不用resume/--last、不读全局会话。`CODEX_HOME`是128MiB临时挂载；auth/config各自只读嵌入；不挂全局`~/.codex`或873MiB历史库。`-o`指向128MiB `/tmp` tmpfs，控制器读取结构化结果后仅将计数、已验证时间/状态、字节数和SHA持久化，自由说明及路径不复制；临时文件随本轮退出清除。

主代理真实CLI烟测发现code-mode-host缺失及workspace-write的bwrap不可用；第三次以同vendor目录的code-mode-host、`never + danger-full-access`在专用Docker中成功执行权威树pwd与API存活查询。因此使用Docker作为明确受信任运维边界，不新增privileged、CAP_SYS_ADMIN或host网络，也不安装宿主工具；这不是权限受限的Codex沙箱。模型可执行范围仍依赖项目约束、明确mount及审计，不应宣传为强隔离。真实完整窗口业务分析仍需另验收，CLI烟测不代表持续任务已生效。

Docker预算0.5CPU/768MiB/192进程沿用已部署服务配置；本次未降低native Codex运行预算。测试及候选构建另限0.3CPU/256MiB，不能以fixture内存当成真实scan峰值。日志轮转2MiB×3。Docker socket给予宿主控制能力，属于受授权项目运维信任边界，不是强安全沙箱；必须以AGENTS及任务prompt限制项目范围，不能宣传为技术隔离。项目根可写挂载用于Git/worktree、既有配置引用及冻结验证；子代理唯一业务写目标仍是指定权威树。扫描只读，修复阶段才按原项目冻结/测试/发布门禁最小修复，不改变财务、账户、源配置、资金许可、模型提供方或其他服务。

已授权Git推进可显式叠加`-f infra/monitor/compose.git.yml`，并设置非秘密路径`EXPERT_GIT_KEY_FILE`、`EXPERT_GIT_KNOWN_HOSTS_FILE`，分别只读挂为`/run/expert-git/key`与`/run/expert-git/known_hosts`。仅复用用户本次明确授权的现有文件；不挂整个SSH目录或全局Git配置，不关闭主机校验，不使用身份代理。`GIT_SSH_COMMAND`固定`IdentitiesOnly=yes`、`BatchMode=yes`、`StrictHostKeyChecking=yes`及指定known_hosts。作者和提交者统一`Project Automation <automation@localhost>`，不使用用户姓名。基础Compose无SSH挂载；未启用或权限不可用时报告阻塞。

## 水位与失败处理

- `.runtime/.../supervisor.lock`：非阻塞flock，第二实例退出3。
- `schedule-v3.json`：枚举所有已闭合UTC窗；`scan_cursor`是最早未首扫窗，`coverage_watermark`只跨越连续analysis_status=complete窗，partial/failed留下真实缺口；`active_task`持有唯一串行槽位。文件在首次初始化就持久化，不再依赖首次成功才产生游标。
- `repair-backlog.json`：以稳定issue_key去重，证据fingerprint变化才重开修复；有确认缺陷和最小复现才可执行。相同fingerprint的稳定外部阻塞进入waiting_evidence，不因重启/时钟/文档HEAD改变重新启动。
- 旧`progress.json`仅作只读兼容校验，旧windows/history/attempts在升级时不重写。53次终态+第54次未完成保留原样，第54导入legacy_interrupted/exhausted，不凭空变为complete。
- `windows/<start_epoch>.json`和`history/<start_epoch>-<attempt>.json`：窗口start/end、状态、attempt、起止时间、退出码、超时及错误类别。metadata不含原文、个人ID或模型自由文字。
- `attempts/<start_epoch>/<attempt>/`：权限700，controller prompt及脱敏events/final/output-summary，禁止提交运行文件。JSONL每行仅白名单type/item_type/id/status/exit_code、原字节数与SHA；未知类型/ID不复制。stdout按流解析、单事件内存上限1MiB，超大/无效事件仅记统计；stderr只记字节数、SHA及枚举失败类别，不生成原始stderr文件。完整command、工具stdout、reasoning、消息和模型自由说明不落盘。
- 调度顺序：最新未首扫闭合窗优先，然后有最小复现、新证据且预算够的ready repair，再最早未首扫catchup，最后到期的有界失败/partial补查。真实已确认缺陷不必等20个旧窗全部扫描完成；新半小时仍抢占下一串行任务。首扫和补查均串行，任何一次失败都可继续其他窗口；每窗总尝试最多3次，冷却并轮转，不永远停在最旧失败窗。
- analysis_status与repair_status分开：全量可访问样本核验完成可以analysis=complete、repair=blocked；缺媒体/未归档内容是partial；读取或模型整体失败是failed。登记unsupported元数据不等于看过其内容。现存相同原图或unsupported已核验仍不可判定时，partial且retryable=false直接exhausted，保留长期健康缺口等待新证据，不重复消耗同一原图。修复/发布受阻不撤回已经完成的分析，也不把partial算完整。
- ready repair在最新闭合窗已首扫后自动执行真实独立子代理；预算取配置上限与距下一半小时边界减15秒的较小值，不足60秒不启动。每个fingerprint最多3次；明确外部阻塞且retryable=false立即等待新证据。只能真实源码修复+验证通过+源码提交才能validated，文档提交不能充数。
- scan prompt只读AGENTS、最新事故短交接末尾最多80行和嵌入的上一窗结构结果，只查本窗新样本；禁止planning/报告/进度/Git写入。scan发生Git提交会被标失败；repair若只有文档提交不能宣称成功。保留全部已有提交，不做重置。
- 已完成结果先写原子journal再提交调度账本；重启回放匹配task token的终态，不重复执行已完成子代理。没有终态的任务记interrupted/130并受总尝试上限约束。
- 子进程独立进程组，准备和执行共享绝对截止，超时/停止TERM后有界KILL，退出124/130留历史。`--health`同时检查90秒心跳、当前deadline、完整coverage滞后/失败/partial；即使process运行，覆盖缺口仍明确不健康。repair阻塞单独计数，不冒充扫描失败或完整修复。

脱敏controller日志另按窗口起点保留72小时，不以修改时间续期；失败metadata/水位长期保留。业务原文、图片引用及详细轨迹只允许代理写`attempts/<start>/<attempt>/raw/`，每文件须在写入前登记`raw-evidence-manifest.json`，格式`{"schema_version":1,"files":[{"path":"raw/opaque.json","posted_at":"2026-11-12T13:00:00Z"}]}`。混合消息/回复/图片使用最早原始posted_at，不得用导出/窗口/编辑时间续期；满72小时的原文不得读取或导出。控制器每次调度检查manifest，等待周期不超过30秒，子代理运行中每15秒检查，按posted_at删已过期注册文件；仅处理本轮raw内非符号链接文件，不递归删除陌生内容。无效manifest留下失败类别并阻止本窗成功水位。该约束不把代理违反manifest约定的任意文件变为可安全清理对象，违规仍须报告修复。公开报告只含脱敏计数、证据定位、提交和限制。

若Docker父进程被强杀，进程命名空间销毁将终止子代理；已经独立创建的Docker测试容器仍按各测试既有trap及专属命名回收，不用全局清理。

## 已有人工分析导入

持续容器保持停止，由主代理以同一受保护环境执行`docker compose -f infra/monitor/compose.yml run --rm --no-deps supervisor --import-analysis <项目内绝对工件路径>`，如需Git overlay沿用相同`-f`参数。此入口只校验并登记分析，不调用模型、不运行修复或业务SQL；仍取得独占状态锁，禁止与其他写者并行。格式如下，每个窗口必须已经闭合并被调度器枚举，计数使用真实审核分母：

```json
{"schema_version":1,"windows":[{
  "window_start":"2026-11-12T18:00:00Z",
  "window_end":"2026-11-12T18:30:00Z",
  "analysis_status":"complete",
  "messages_examined":7,"unresolved_examined":7,
  "evidence_file":"<项目内已脱敏证据文件绝对路径>",
  "evidence_sha256":"<证据文件实际64位SHA256>"
}]}
```

支持complete或partial，逐项核对证据路径、大小、SHA及计数，整份合法后才更新；同一真实证据可支持多窗。工件SHA回执保存在`imports/`，原失败history/windows及Git历史不改写。人工partial标记manual_verified/exhausted，只表示本次现存内容已核验且缺口仍在，不自动反复调模型尝试读取不存在的内容；有新证据时再显式导入更新。不能将unsupported元数据当作已读原文或非推荐。真实17–19四窗工件位于`reports/expert-review-incident-20261113-import-analysis.json`，状态为partial/partial/complete/partial；本实施阶段未执行导入。

## 最小验证

新测试覆盖窗口/水位/失败/锁、posted_at保留及越界、脱敏流式输出、有界内存、真实无模型双pipe与子代理命令，不调用模型或生产业务。应用测试全部在Docker中运行：

```sh
docker run --rm --network none --cpus .3 --memory 256m --pids-limit 64 --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=32m -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD:/source:ro" -w /source --entrypoint python \
  sha256:7c63286d441335da47509886c40fdde924878ef8b85d53631cfde6a7efa6b7d1 \
  -m unittest tests.test_expert_iteration_supervisor tests.test_expert_iteration_scheduler -q
```

部署前另验证Compose渲染、精确镜像、只读认证、真实独立子代理JSON结果及退出码；首完整窗口结束后检查报告/水位/超时和真实样本，才可称持续子代理已生效。模型外部失败、没有样本、20秒超标都必须如实报告。

## 本次调度事故修复验证

2026-11-12 23:57 UTC：状态为代码已写、隔离验证通过，尚未构建v3候选镜像、启动持续容器或执行真实模型窗口。按“最小验证”原命令运行两模块共52项，1.481秒，退出0；基础Compose与Git overlay以非秘密路径变量执行`docker compose -f infra/monitor/compose.yml -f infra/monitor/compose.git.yml config --quiet`退出0；`git diff --check -- infra/monitor tests/test_expert_iteration_supervisor.py tests/test_expert_iteration_scheduler.py`退出0。此阶段只写控制器、调度模块、schema、Compose、README和对应测试，未改业务/DB/资金/来源配置、未提交、未部署。

新增测试覆盖53次失败与第54次中断的真实结构、升级保留证据、每窗catchup、最新窗优先于修复、已确认修复优先于20窗历史积压、稳定partial不重烧原图、最多3次与冷却轮转、修复新证据指纹、跨窗预算、独占lane、journal崩溃回放、真实无模型子进程超时124、停止130、准备与执行共享截止、Git无源码增量不得验证、真实main连续三窗调度及人工导入SHA防篡改。完整coverage滞后或partial长期存在时，存活进程仍返回不健康。

另用同一现成镜像、0.3CPU/256MiB、只读项目挂载及/tmp临时状态进行两项无模型验证，均退出0：读取真实旧runtime后仅在内存模拟，53条history和attempt54原样保留，20个新首扫窗包含17–19四窗，最先选择23:00–23:30，旧13:00窗维持legacy_interrupted/exhausted且coverage仍停13:00；将accuracy实际SHA绑定工件导入临时状态，得到partial/partial/complete/partial、manual_verified=true、单份回执，未把缺口变为complete。真实持续runtime、业务库与生产文件均未写入。旧容器本阶段核验仍为exited。

冻结源码SHA256：控制器`2dcacdc4dd709ed53f7082646712319b5dc3ded80bebd25687817a0740b3c997`，调度模块`0477fb35cf35b09eaeec57402ae03d35651ccd01e3bac2e671b1c9e098367fc5`，schema`5245636434d0a89b8243946d3a32f212ba18f8ca4ac0955503831c0d164397d2`，新增调度测试`d99eb5fa152c90adb43dbaf87e8a29e88eb5222e072299de2874a0e75524a259`。下一步由主代理统一冻结/构建，核对镜像内控制器/调度模块/schema与对应SHA一致，使用独立烟测状态目录执行真实scan；确认不会改Git且metadata/health如实记录，再停止其他写者、登记人工四窗并串行启动。真实native内存峰值、模型完整输出及持续30分钟周期仍须该阶段验证，fixture不能证明生产达标。

## 首次候选冻结验证记录（已被日志隐私修复替代）

2026-11-12 13:15 UTC：状态为隔离验证通过，真实子代理、持续调度与发布验收尚未运行。当时构建使用`.4CPU/384MiB`，退出0，最终镜像为`sha256:3c1e1a8204be8e88a4eb0242e7cf59318459a5d0e4ab4e6d5b9bf04edd35ebbd`，标签`ai-football-expert-monitor:20261112.1`。控制器、结果schema及两份依赖快照逐项比较镜像和冻结源码SHA，四项全部一致；控制器`--help`退出0。

上方Docker单元命令以`-v`运行共19项，0.135秒，退出0。Compose用非秘密路径环境变量执行`docker compose -f infra/monitor/compose.yml config --quiet`退出0，`git diff --check -- infra/monitor tests/test_expert_iteration_supervisor.py`退出0。以最终镜像、`--network none --cpus .25 --memory 384m --read-only`及32MiB临时目录，只读挂现有Codex和Compose插件后执行`git --version && ssh -V && docker --version && docker compose version && codex exec --help`退出0；仅验证工具入口，未调用模型、未连接生产数据库、未启动长期容器。

源码SHA256为`ebe42c67f18dd8d41b7fe676d586414297c0f8ca48a59f4e0539afafd8820574`，schema为`569418de92a306dd08efe75ef645bbdcba875fbe9a3156638ae70b342a38ead7`，Debian快照为`839789347c75ff6957b210e7ecf92030672d1b207452702066210f070a6f4e4b`，Python快照为`84602771a69520440cfca678c748fa2161ba6ecee22d2157429f923f6621e88e`。下一步由主代理注入项目最小认证/provider配置，用独立状态目录完成真实闭合窗口烟测，核验Docker命令沙箱可用与结构化结果，再串行启动持续容器。

## 日志隐私修复的历史冻结（v3调度前）

2026-11-12 13:29 UTC：当时控制器源码SHA256为`2f577976f79149cfb89a37b99c209fd92902f4387f98b6116122624f93bf5784`。当时构建tag为`ai-football-expert-monitor:20261112.2`，限制`.4CPU/384MiB`，退出0，镜像`sha256:7c63286d441335da47509886c40fdde924878ef8b85d53631cfde6a7efa6b7d1`替代首次`.1`候选。它不含本次v3修复，不能把旧镜像重新启动当作事故修复完成；这里只将其作为现成Docker测试运行时。镜像内当时控制器/schema/两份依赖快照与冻结源码SHA逐项比较全部一致。

新增六项隐私/TTL行为测试后，API测试镜像运行25项0.513秒退出0；最终控制器成品镜像以`--network none --cpus .25 --memory 256m --read-only --tmpfs /tmp:rw,nosuid,nodev,size=32m`运行同25项0.195秒退出0。包含真实无模型子进程双pipe输出脱敏、1MiB事件内存边界、最终自由文本去除、旧回复按posted_at在新窗过期和恶意manifest不得越界。基础及可选Git overlay共同执行`docker compose ... config --quiet`退出0，`git diff --check`退出0。未执行模型、生产数据库、长期容器或提交操作；真实CLI smoke由主代理串行完成，完整窗口分析和持续任务验收仍归下一阶段。
