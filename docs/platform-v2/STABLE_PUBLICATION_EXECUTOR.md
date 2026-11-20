# 正式发行执行器与证据保管

更新时间：2026-10-27。此包实现发布动作的失败关闭路径；本轮没有签名 APK、生成任何签名密钥、上传 Release 或创建标签。离线测试只验证协议顺序，不能用作正式设备或生产证明。

## 输入、信任和职责

现有只读计划器验证正式语义版本、干净源码、已签 APK、六组件不可变镜像、配置/迁移摘要、八类验收证据。新增执行器重新计算计划，验证所有八类证据明确 synthetic=false、fixture=false，且 Android 安装/升级记录绑定精确 APK 哈希。

JSON 声明真实环境不是事实证明。发布还必须取得独立保管人的 Ed25519 签署；公钥由受保护 stable-release 环境的变量 STABLE_RELEASE_TRUSTED_PUBLIC_KEY 提供，不能从下载包读取。签署载荷只允许 format、repository、source_commit、version、plan_sha256、evidence_manifest_sha256、real_environment、synthetic、approval_id、approved_by、issued_at、expires_at。格式标识为 football-stable-approval-v1；签署内容为 UTF-8 JSON，键排序、无空白、非 ASCII 不转义，不允许 NaN。有效期最多24小时。

保管人负责核验真实设备、生产只读、恢复等证据的来源和脱敏；代码只能证明签署身份、摘要、范围与时效一致，不能自动证明人写的报告是真实的。无签署、签署错误、过期、合成夹具或缺少范围都会拒绝。本库只验证公钥签署，未提供密钥创建或签署指令。

## 可执行工作流

受保护工作流只在 main 且输入源码等于该次 GitHub SHA 时运行；stable-release 环境必须配置有审批人、禁止自审、只允许受保护分支。执行器通过 GitHub API再次核验这些条件，以及受保护 main 仍指向精确源码。仓库默认分支不是 main 或环境保护不可读均拒绝。

输入 evidence_run_id 必须是同仓库成功完成的 main 工作流运行，源码与当前发行一致。该运行的 stable-approved-bundle 产物按下表放置；下载动作固定到已核对的完整 actions 提交，随后执行器对所有文件再次校验，不信任下载文件自带的“通过”声明。

| 包内路径 | 内容 |
| --- | --- |
| release-plan.json | 现有计划器基于本次完整证据输出 |
| signing-manifest.json | 正式签署清单，含源码、版本、证书、APK摘要 |
| app.apk | 与清单及设备证据完全相同的正式签署 APK |
| package-verification.txt | 原 apksigner 与 aapt 输出 |
| source-worktree-status.txt | 源码干净记录，必须空 |
| image-inspect.json | 六组件镜像 reference 与原 inspection |
| evidence/ | 八范围 JSON 记录 |
| release-notes.md | 审阅后的版本说明 |
| release-approval.json | 外部签署的载荷与 signature_ed25519 |
| 证据引用路径 | 记录引用的去敏日志/验收数据，按仓库相对路径保留 |

证据中的 artifact.path 以仓库根为基准，例如 .runtime/stable-bundle/proofs/device.json。保管人必须在最终包路径下生成并签署计划；不能移动路径后沿用旧计划。当前候选签名工作流只产候选 APK，不会自动构造真实设备证据或这个保管人包；它们是明确剩余真实环境依赖。

工作流在不联网的 Docker 内先验证外部保管证明，再按已验证列表逐个拉取六个不可变镜像，重新核对内容 ID、源码和版本标签。Android工具链在 Docker内对实际APK重新执行 apksigner verify 与 aapt dump badging，无私钥。上述新输出、当前干净工作树、原证据包运行身份再次验证后才能进入写操作。

## 发布状态与失败处理

1. 标签和 Release 均必须不存在；只创建精确源码的新轻量标签，没有强制更新或删除接口。
2. 创建 draft Release，逐个上传 APK、计划、镜像覆盖、审批记录、证据清单、证据 ZIP、版本说明及签署元数据。
3. 每个资产及完整资产列表均核对 GitHub state、字节数和 SHA-256；随后重读 main 与标签，最后单次将 draft 公开。
4. 公开后重读 Release及全部资产再报告 published=true；任何网络、权限或回读异常均不盲重试，也不声称成功。

资产单个不超过64MiB、合计128MiB；证据最多32条记录、每记录16个引用、去重后64个文件/32MiB，单证据文件16MiB。证据 ZIP按路径排序、固定时间戳，包含记录与引用，不能只发摘要而丢掉复核依据。超限拒绝，不截断。

写操作失败保留已创建的不可变标签及 draft，供管理员核对；执行器不会删除半成品、覆盖同标签或自动换版本继续。重新发行须审查失败点，并使用新版本/新审批；不得为了通过删除标签。工具仅允许固定 GitHub API 路径，拒绝重定向、任意主机、DELETE、force-ref和非白名单方法。令牌不出现在输出中。

## 本轮验证

冻结快照 Docker、0.25 CPU/256MiB、无网络：python -m unittest tests.test_stable_release_execute tests.test_stable_release_plan -v，17项通过，2.704秒，exit0。含公开 RFC8032签署向量及篡改失败、缺新复验/脏源码时不创建网络传输；阳性发布用假 GitHub 与被明确替换的保管验证，不伪装成真实签署。日志 .runtime/stable-executor-20261027/tests-final4.log，SHA-256 ba287cf575a1e023cd0957375d63657242d9b6aeded3476d22ee87bae7e63223。全套主线仍需统一重跑。两个workflow actionlint exit0。未执行正式 workflow，正式环境配置/签署者/设备/升级/生产证据齐备前不可发行稳定版。
