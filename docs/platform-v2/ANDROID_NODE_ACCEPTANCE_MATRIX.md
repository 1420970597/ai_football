# Android 节点与发布资产验收矩阵

核验日期：2026-10-27；只读证据包：.runtime/android-node-audit-20261027/。本记录不修改任务勾选，也不把 JVM、浏览器或 APK 构建当成真机验收。

## 节点证据与未验范围

| 节点 | 已实现与本轮离线证据 | 尚未完成的真实验收或代码缺口 |
| --- | --- | --- |
| P36-01 | 手动 HTTPS 地址、服务身份/最低客户端版本校验、域名版本确认、Keystore AES-GCM 配对存储；17 项 JVM/TLS 测试通过 | 此冻结快照未实现扫码；需补扫码候选输入，仍需设备验证探测、确认、进程重建后的配置保留 |
| P36-02 | 当前同源认证页面、危险 scheme 阻断、外链确认后系统浏览器；JVM 覆盖域名后缀/端口/凭据 URI | 实际 WebView 登录、外链手势与 Cookie 隔离未在本轮设备执行 |
| P36-03 | 禁止文件/内容访问、混合内容、第三方 Cookie、生产调试；TLS 错误始终取消；Lint 无错误 | 正式包设备验证及证书失败界面未验 |
| P36-04 | 清除本机登录、换服务时清 Cookie、WebStorage、缓存及历史；配对与网页凭据分离 | 真设备重启、清理回调、双身份登录与跨域隔离未验 |
| P36-05 | 主文档网络/HTTP/SSL 失败、重试、迟到完成回调保护；相关 JVM 状态测试通过 | 真设备离线恢复、渲染器故障及实际错误界面未验 |
| P36-06 | 原生安全区处理、滚动配对/离线界面、历史返回 | 此快照系统返回不先关闭 HTML dialog；需补可取消关闭与异步防重复。横屏、大字体、安全区须真设备验收 |
| P36-07 | 安装/升级/离线验收工具与可执行入口已有 | 本轮 ADB 无授权设备，不得勾选。旧模拟器安装包含 ANR，不能替代当前完整链路通过 |
| P37-01 | 固定 SDK 35、构建工具 35.0.0、Gradle/AGP/JDK、依赖锁；完整 Docker 构建通过 | 不替代签名及设备验证 |
| P37-02 | 包名、版本码/名称、服务地址策略、产物清单校验；26 项发布/设备编排契约通过 | 正式版本仍需统一源码与发布资产 |
| P37-03 | 受保护候选签名工作流及注入校验实现，测试只用明确合成夹具 | 未收到可验证正式签名与授权设备；不可把合成签名包称正式包 |
| P37-04 | 默认仅输出 debug 和明确 unsigned APK | 保持候选状态，不发布 stable |
| P37-05 | apksigner、aapt、精确权限清单、SHA-256、源码和镜像关联工具已有；本轮构建与真实开发 Release 资产核对通过 | 正式签名身份及最终版本同提交资产待验 |
| P37-06 | 已校验清单驱动升级、版本/签名/摘要核对、fake ADB 行为测试 | fake ADB 不是安装；上一候选升级、会话配对保留、卸载重装、签名不一致仍需设备 |

## 本轮命令与结果

1. bash android/build-candidate.sh，日志 .runtime/android-node-audit-20261027/build-candidate.log，退出 0；CPU 1、内存 1536 MiB、Gradle worker 1，90 个任务；17 项 JVM/TLS 测试、Lint（0 error/1 warning）、debug 与 unsigned release 均成功。中途依主线资源窗口暂停容器，恢复后完成，墙钟时长不能作性能结论。
2. 固定 Python 镜像、只读冻结快照、无网络、CPU 0.25/内存 256 MiB：python -m unittest tests.android.test_release_policy tests.android.test_device_contract tests.test_release_manifest tests.test_stable_release_plan -v，26 项通过，退出 0；日志 contracts.log。
3. 已有 device-tools 镜像、无网络、CPU 0.25/内存 256 MiB 执行 device_preflight.py --host 127.0.0.1：退出 2；ADB 命令退出 0，授权/离线/未授权设备均为 0，KVM 不可用，原因 no_authorized_device。记录 preflight.json，不是应用失败。

构建源父提交 0c3dfd680d67db28ca05e904e67f534af8bbe045；构建前 Android/相关测试工作树无改动。debug SHA-256：6deb676736aafdff5daeb3e2b998878370ad6d11b033c55d045423de57822e6f；unsigned SHA-256：b7e9b446432b268a4a174281a1edaea7aac0c7c62152faddd8ac03ff5d1978db。

最新测试 XML 位于 android/build/artifacts/unit-results/testDebugUnitTest/；旧包装器复制到已存在目录时产生嵌套，根级 XML 为旧记录，核验不得误读。

## 远端 Release 与 CI

GitHub 唯一可见 Release 为 v2.0.0-dev，prerelease，2026-10-26 发布，源码提交 37c511de9ae33d24e756f134db0a98ee0fe5cf1e。实际下载 debug 35207 B、unsigned 27436 B，均与发布清单摘要匹配；SDK apksigner/aapt 验证退出 0，版本码 20000，版本名 2.0.0-dev，debug 包名含 .dev，权限仅 INTERNET/ACCESS_NETWORK_STATE。debug 为 Android Debug 签名；unsigned 不能宣称正式可安装版本。

远端 debug SHA-256：0d4454f29e8410f4332b7705a52e426397ae836e98a33bfabc29da3f8a31ab31；unsigned 与上文相同。发布清单明确 production_ready=false。Release 不是当前最新源码：读取时 main 为 8a13716962d2022ea16b6083f13c840c57ed42bc，活动分支为 0c3dfd680d67db28ca05e904e67f534af8bbe045；主线可能继续更新，以上仅为核验时点。

Android 边界 CI 40553423394 和平台 CI 40553423272 在提交 0c3dfd6 均成功，后者 Android 构建作业通过；开发发布 CI 40331095572 对应旧 37c511d 成功。CI 绿色与可下载开发资产是已有实现证据，不是当前稳定版或真机完整验收。

## 下一独立包

不增加摄像头权限和提供方依赖，接入明确允许的 ZXing 兼容外部扫码器 SCAN Intent；结果只作候选，仍走现有 HTTPS 探测与域名/版本确认。缺少扫码器与取消清晰回到手动输入。返回仅对当前同源页面执行固定脚本，先派发可取消的 cancel，再调用 dialog.close 触发既有清理；无 dialog 才返回历史，超时与迟到回调不得重复回退。新包另记证据，仍保留设备未验限制。


## 后续实现纠偏（E101）

上表是扫码/返回独立包之前的审计快照，不代表当前仍缺实现。当前已补外部受控扫码器候选输入和同源HTML弹窗优先返回；23项JVM/TLS、6场真实Chromium脚本、26项发布契约通过及全部源码摘要一致，详见ANDROID_PAIRING_NAVIGATION。P36-01/P36-06的扫码/弹窗实现不再缺，真实设备、横屏/大字及正式签名仍待，故不整项虚勾。
