# Android 签名候选与稳定发布门禁

全部构建、签名、包验证和测试在 Docker 内运行。签名入口只生成明确命名的 `signed-candidate`，不会创建正式 GitHub Release。开发入口继续生成 Debug 与 unsigned 两类产物，互不覆盖。

## 外部签名材料

由签名保管人提供稳定密钥、证书 SHA-256 及对应密码，放在项目内被忽略的 `.runtime/` 私有子目录。目录权限为 0700，每份输入为 0600 普通文件，禁止符号链接：

- `signing.jks`：既有签名密钥库。
- `store-password`、`key-password`：仅含相应密码的文件。
- `key-alias`：签名别名，不包含换行。

设置 `ANDROID_RELEASE_VERSION`、`ANDROID_RELEASE_VERSION_CODE`、`ANDROID_SIGNING_DIRECTORY`、`ANDROID_SIGNER_SHA256` 后运行 `bash android/build-signed-candidate.sh`。版本只接受正式语义版本或带编号的 rc；版本码由发布负责人按既有安装版本递增。入口拒绝覆盖已有签名候选目录，先保留或明确移除上一候选后再构建。密钥通过只读目录挂载，不作为 Docker build 参数、镜像层或日志内容。密码使用 apksigner 的文件输入。临时构建容器结束后移除；产物中只保留 APK、元数据、源码哈希、签名证书摘要和测试记录。

签名验证必须匹配外部登记证书、正式包标识、所选版本码/版本名及两项已审核网络权限；拒绝 Android Debug、可调试、testOnly 和额外权限。未提交源码可以构建签名候选，但清单标记 `source_clean=false`，不能通过稳定门禁。

手动 GitHub 工作流使用受保护的 `android-signing` 环境及 `ANDROID_KEYSTORE_BASE64`、`ANDROID_STORE_PASSWORD`、`ANDROID_KEY_PASSWORD`、`ANDROID_KEY_ALIAS` 四项环境秘密。工作流使用只读内容权限，上传签名候选，不发布版本；最终步骤删除本次暂存的四份材料。不得为跑通工作流而临时生成正式身份或降低审批环境要求。

## 正式发布证据

稳定门禁要求当前源码提交、版本、七天内实际命令及退出码、真实授权环境标记和可核验本地产物哈希。八类证据为生产 HTTPS、远端恢复、只读采集、专家审核、统计、双端浏览器、Android 设备与 Android 升级。记录目录必须在项目内，产物不得越界或为符号链接。任一失败、缺失、过期、提交不符、哈希不符、候选版本或脏树都拒绝。

每类 JSON 记录包含 `scope`、`status=passed`、`exit_code=0`、`source_commit`、`version`、`real_environment=true`、UTC `checked_at`、实际 `command` 和 `artifacts`（项目相对 `path` 与 `sha256`）。两项 Android 证据还必须包含与待发包完全一致的 `apk_sha256`，不能拿另一个签名包的设备测试代替。这只是可审查的发布门槛，不会将合成测试记录变成真实证据；正式发布人员仍须复核原始产物。

运行 `python infra/android/release_policy.py` 必须在 Docker 中，传入版本、版本码、提交、包校验输出、外部证书摘要、APK、工作树记录和证据目录。只有完整签名与真实证据通过才输出 `stable-eligible`；候选签名通过仅输出 `signed-candidate` 且 `production_ready=false`。

开发清单和签名清单共用逐包权限策略：每个 aapt 包块均必须恰有 INTERNET、ACCESS_NETWORK_STATE 两项，额外、遗漏、重复、畸形和 SDK 限定的额外权限均拒绝。开发清单的每个 APK 记录包名、版本名与版本码，设备工具不从文件名猜身份。

## 入口与设备验收

`sh infra/android/check-edge.sh` 只启动本任务的临时内部 Docker 网络、无状态 API 与当前 Nginx 配置，不访问生产。实际验证内部路径、跨域响应、请求体上限和突发限流；退出即清理本任务容器和网络。

`sh infra/android/check-device.sh` 从固定 SDK 构建精简 Python/ADB 工具镜像（ADB 字节摘要固定），输出设备预检 JSON，失败退出 2；不依赖历史临时模拟器镜像，不把设备缺失当通过。可通过 `ANDROID_DEVICE_NETWORK`、`ANDROID_ADB_HOST`、`ANDROID_DEVICE_SERIAL` 选择项目自有网络中的受控设备。只有 ready 后才在同一受限工具容器执行已有 UI 冒烟脚本的各阶段。脚本支持显式 ADB 主机、端口、设备、样本 HTTPS 来源和输出目录；完整验收还须追加实际旋转、大字号、换服务和升级链路，不能以预检或安装成功勾选设备完成。

设备阶段入口现在必须显式传入 `--manifest`、`--apk`、`--package-output`、`--host`、`--serial`、`--origin`、`--output`，所有产物和输出限制在项目内。签名包使用 signing-manifest；开发 Debug 使用开发发布清单，旧清单缺少包身份时必须重新生成，未签名包不可运行设备验收。工具先核对清单哈希、签名身份、元数据和权限，再从清单推导启动组件；设备端还读取选定包的安装版本和 base.apk 字节，要求摘要与待测 APK 完全相同。ADB 返回错误和版本不符都保存失败记录，不存原始 stderr 或账号信息。

`upgrade` 阶段额外要求三项 `--previous-manifest`、`--previous-apk`、`--previous-package-output`；先核对同包同签名和递增版本码，核验旧安装字节，执行 `adb install -r`，再次核验当前版本与字节并检查配对页保留。输出保留前后版本、精确摘要、操作退出码和截图哈希；未覆盖会话连续性、旋转、大字体、换服务、断网、卸载重装与降级拒绝仍逐项列为未验收。这些阶段只针对隔离页面，始终标记 `real_environment=false`，不会自动生成正式设备证据。
