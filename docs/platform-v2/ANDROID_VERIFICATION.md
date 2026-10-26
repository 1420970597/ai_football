# Android 客户端开发与验证记录

更新时间：2026-10-26 11:23 UTC。状态：17 项隔离测试、Lint、两种 APK 及独立候选构建入口通过；APK 安装成功，设备交互验收被模拟器系统 ANR 阻断。此记录不能替代真实设备验收或正式发布。

## 已实现的行为

- 首次启动没有默认服务地址，要求输入 HTTPS 站点根地址。拒绝明文、账号密码、页面路径、查询凭据、片段和不安全 URI。
- 使用系统证书验证连接，独立请求公开客户端契约，不携带 WebView Cookie、不跟随重定向。核验产品协议、API 版本、最低 Android 版本和响应大小，展示域名及服务版本后由用户确认配对。
- 产品编号仅表示协议兼容，不能证明运营方身份；身份依据 HTTPS 域名及系统信任证书，用户仍须核对管理员提供的地址。
- 配对地址由 Android Keystore AES/GCM 保护，用户与管理员会话保留在应用 WebView 的 HttpOnly Cookie 中。关闭备份、文件及内容访问、明文和混合内容、第三方 Cookie、任意新窗口与 JavaScript 桥。
- 只有同源 HTTPS 页面进入登录 WebView。用户点击的其他 HTTPS 域名在确认后交给外部浏览器；脚本跳转、非 HTTPS 协议和危险 URI 被阻止。
- 主文档网络、HTTP、证书或渲染进程错误均显示阻挡页面，迟到的页面完成事件不能伪装恢复。网络恢复后明确提示重试；重试重新拉取当前页面，不生成离线投注。
- 服务切换及“清除本机登录”删除本应用 WebView Cookie、站点存储及缓存，不影响设备其他浏览器。清除本机 Cookie 不等于撤销服务器上全部会话；撤销仍使用账户页面的服务端功能。
- 原生配对、离线、菜单有滚动布局、至少 48dp 操作区域及状态朗读；适配系统栏和刘海安全区域。实际大字号/设备交互仍待验收。
- Debug 使用独立应用编号后缀，不覆盖正式客户端；无外部签名材料，发布变体保持未签名。

## 工具链与依赖

所有步骤在 Docker 中完成，没有访问宿主 SDK、签名或外部项目目录。基础 Gradle 8.9.0/JDK 17 镜像固定到摘要；Android Gradle Plugin 8.7.3、Android API 35 revision 2、Build Tools 35.0.0、Platform Tools 37.0.1 和命令工具归档固定版本及 SHA-256。依赖锁与 Gradle 校验元数据记录构建及测试传递依赖，复验不使用更新校验元数据选项。

工具来源与兼容性依据：

- [Android 官方 Gradle 8.7 兼容性表](https://developer.android.com/build/releases/agp-8-7-0-release-notes)：Gradle 8.9、JDK 17、最高 API 35。
- [Android 官方命令工具与校验值](https://developer.android.com/studio)：命令工具归档的版本及 SHA-256。
- [官方 SDK 仓库元数据](https://dl.google.com/android/repository/repository2-3.xml)：平台和构建工具归档来源；构建同时核验固定 SHA-256。
- [Android 官方 URI 加载风险](https://developer.android.com/privacy-and-security/risks/unsafe-uri-loading)：同时验证协议和完整主机名，不使用域名后缀匹配。

## 实际命令与阶段证据

工具链构建：

```sh
docker build --target toolchain -f android/Dockerfile -t ai-football-android-toolchain:v2.0.0-dev .
```

首次及固定 SDK 归档后的第二次均退出码 0。第二次镜像标识：`sha256:d2888786999468a15cca23e98824bbc65f4315238db1cbdadaa93f4bcb52ee12`。SDK 工具提示旧 sdkmanager 入口已弃用；本项目仍使用其兼容入口接受 SDK 许可，实际组件从固定归档安装。

首次冻结副本构建使用 `--write-locks --write-verification-metadata sha256` 建立依赖锁和校验记录，执行单元测试、Lint、开发包与未签名发布包，退出码 0，耗时 2 分 33 秒。当时包含 11 项纯策略测试，随后增加真实本地 HTTPS 行为测试及渲染进程恢复处理，因此最终以以下严格复验为准。

```sh
docker run --name ai-football-v2-android-verify --cpus=1 --memory=1536m \
  -e GRADLE_USER_HOME=/workspace/gradle-cache \
  -v /root/ai_football:/source:ro \
  --entrypoint sh ai-football-android-toolchain:v2.0.0-dev -c \
  'mkdir -p /workspace/frozen/tests && cp -a /source/android /workspace/frozen/android && cp -a /source/tests/android /workspace/frozen/tests/android && cd /workspace/frozen/android && gradle --no-daemon --max-workers=1 --console=plain :app:testDebugUnitTest :app:lintDebug :app:assembleDebug :app:assembleRelease'
```

资源预算：实际编译、Lint 和测试容器限制 1 CPU、1536MiB；Gradle 单工作线程、最大堆 768MiB。首轮观察约 849MiB；最终资源及产物校验值待补充。

严格复验首轮在 1 分 51 秒退出码 1：测试使用的 JDK 专有 HttpServer 模块不在 Android 裁剪后的编译系统模块中。已改用标准 SSLServerSocket 构建本地 TLS 测试，不删减证书及重定向验收，修复后重跑同一组合。应用自身编译步骤在该次通过；整体验证仍按失败记录。

## 尚未完成的验收

- 尚未连接真实设备/模拟器，不能声称 APK 已安装或真实 WebView 已运行。原生配对、Keystore、系统返回、旋转、弱网、放大字体和 Cookie 隔离需实际设备验证。
- 仅使用仓库内测试与隔离本地 TLS 服务器，无生产服务、真实账户或真实投注。生产 HTTPS 域名和证书尚需与部署阶段联调。
- 当前为开发 APK 和未签名发布 APK；正式密钥、升级签名连续性、Release 资产及镜像远端发布由主流程完成。

## 冻结复验与设备进度（11:08 UTC）

增加缺失 WebView 的明确恢复界面后，最终冻结容器 `ai-football-v2-android-sealed` 执行同一 Gradle 组合：退出码 0，1 分 10 秒；17 项测试（11 策略、6 本地 TLS 网络行为）全部通过。Lint 0 错误，1 项兼容性提示：API33 预测返回声明在旧版本忽略，旧系统仍由普通返回实现。上一次 4 项告警中的备份规则、可访问性标注和重复背景已修复。

本轮产物位于仓库 `android/build/artifacts/`（被 Git 忽略，不含正式密钥）：

| 产物 | SHA-256 | 用途 |
| --- | --- | --- |
| `ballnews-2.0.0-dev-debug.apk` | `7679834ecf1451abf322b1ce8b302f46410566436e7e5eb076a0f8712e92d31a` | 临时 Android Debug 签名，独立 `.dev` 应用，仅开发验收 |
| `ballnews-2.0.0-dev-unsigned.apk` | `b7e9b446432b268a4a174281a1edaea7aac0c7c62152faddd8ac03ff5d1978db` | 未签名发布变体，不能作为正式可安装包发布 |

`apksigner verify --verbose --print-certs` 对开发包退出码 0，验证 v2 签名；证书明确为 Android Debug。`aapt` 确认最低 API26、目标 API35，仅 INTERNET 与 ACCESS_NETWORK_STATE 权限。正式变体关闭调试，开发变体允许 WebView 开发调试。源码仍待主流程阶段提交，后续正式版本应在已提交源码上重新生成清单。

新增候选构建入口 `android/build-candidate.sh`：宿主仅调用 Docker、Git 和文件复制；容器内冻结构建输入、运行测试/Lint/编译、输出源码逐项哈希、父提交及脏树状态、包元数据、签名验证及校验值。Dockerfile 专用忽略规则只发送 Android 源码、锁和测试，不打包缓存、APK、运行数据或其他服务。该入口 Bash 语法检查退出码 0；最终独立入口的全流程再验收仍待完成，不能仅凭语法检查称可复现。

模拟器证据与失败：

1. 官方 Emulator 37.2.12 初始工具容器缺 X11/Xi 库，退出码 127；在本项目临时容器补齐运行依赖后，工具版本核验退出码 0。
2. 官方 API26 AOSP r1 镜像在 1GiB 上限下，模拟器自行提升 guest RAM 至1536MiB，引发 cgroup OOM；`memory.events` 明确 `oom_kill=1`。384MiB 参数被模拟器覆盖，因此没有继续重复相同参数。
3. 经主流程授权，仅该模拟器调整至 2GiB（CPU仍0.75）；官方 AOSP 镜像完成启动，开发包 `adb install -r` 返回 Success、退出码 0。
4. 首次 UI 验收发现 AOSP r1 镜像未配置有效 WebView provider（`dumpsys webviewupdate` 无可用实现），布局启动失败。项目新增明确的网页组件缺失提示，已重新冻结编译；设备更换官方 Google APIs API26 r16 镜像继续验收。测试不能把安装成功等同于配对/登录通过。

测试镜像来源均为 Google 官方仓库；归档校验：Emulator `b08fc43d8608d2955f607f1b287beb525041e730086bdca3af152accac3af9c1`，AOSP26 `78a79eb02751b19b6626b785f05d5b0ab3a283596fe9d29e54905090a36fe77d`，Google APIs26 `cd19a220610518b107ba8bca5da16088bb07d5ffedb53106ca9caf9adda06a4d`。这些测试工具/会话/证书只存在任务临时容器中，不进入应用信任配置，不影响生产 TLS 策略。

## 最终交付与设备限制（11:23 UTC）

独立入口 `./android/build-candidate.sh` 已完整执行成功，退出码 0（Gradle 2 分 35 秒），没有依赖临时 commit 的依赖缓存镜像。17 项单元/TLS 网络测试零失败、零跳过，Lint 0 错误。输入只挂本项目 Android 及测试目录，容器内复制冻结后再运行，输出源文件清单、父提交与脏树状态，证明此次源码尚未正式提交，不能冒称与已发布提交一致。

入口输出为当前开发候选：

- 开发 APK：`233672fe57d67da7695290bd05b2b73b8637a383e5aff52f6051c323927de975`。
- 未签名发布 APK：`b7e9b446432b268a4a174281a1edaea7aac0c7c62152faddd8ac03ff5d1978db`（与 sealed 轮完全相同）。
- 开发签名 SHA-256：`b4bc55064651a417411a4dbb586442b7e1c2a480c1105f36a3ed7f6336019c73`，仅临时 Debug 身份。临时构建容器每次生成独立 Debug 签名，因此不同构建的开发包不能承诺直接覆盖升级；正式签名和升级连续性仍未配置，未伪造签名密钥。

设备最后复验保留真实失败：Google APIs26 镜像有有效 Chrome69 WebView provider；2GiB 上限仍发生第二次 OOM。进一步用 QEMU RAM768MiB 覆盖后，实际 guest 内存 `758420kB`、RSS约1.38–1.61GiB，无新增 OOM，系统启动并 `adb install -r` 再次成功。UI 自动化发现的是 Android **System UI isn't responding** 系统 ANR 对话框，原生首次配对断言未通过；没有把系统错误截图当应用通过截图。

- [系统 ANR 原始截图](../../reports/android-device/blocked-system-ui-anr.png)
- [系统窗口原始 XML](../../reports/android-device/blocked-system-ui-anr.xml)

设备样本证书与账号只存在任务容器，生产 APK 继续只信任系统 CA；没有放宽证书规则。由于系统 ANR，尚未执行成功的项目包括：实际原生配对、WebView 登录阅读、会话过期、断网恢复、更换服务、注销本机登录、旋转和大字号。`tests/android/device_smoke.py` 与本地 TLS 样本已准备，但只能将对应场景标为待设备验收，不应勾选 P36-07 或 P37-06。测试模拟器已停止以释放资源，后续需要具备足够资源/加速的模拟器或受控真机继续。

## 候选包元数据策略（2026-10-26）

`infra/release/manifest.py` 现在从 `aapt dump badging` 输出中选择 release 变体，严格要求包名 `com.aifootball.platform`、版本码 `20000`、版本名 `2.0.0-dev`；必须包含 `INTERNET` 和 `ACCESS_NETWORK_STATE`，拒绝读取/写入外部存储及读取电话状态权限。候选入口同时对 debug 和 unsigned 包输出 aapt 信息，并保留 apksigner v2、父提交、脏树状态和 SHA-256。该策略不等于正式签名或设备升级验收。
