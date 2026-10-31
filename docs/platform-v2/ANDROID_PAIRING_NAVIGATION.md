# Android 扫码配对与系统返回

更新日期：2026-10-28。独立改动范围为 Android 客户端、对应验证与本说明。按用户要求支持显式 HTTP/HTTPS 配对；签名身份及服务接口协议不变。

## 扫码入口与信任链

连接服务页面新增“扫描服务地址二维码”，先展示会打开外部扫码器的说明，再发送显式包限制的 ZXing SCAN Intent。当前允许的包是 ZXing Barcode Scanner（com.google.zxing.client.android）与 Barcode Scanner+（com.srowen.bs.android），请求模式仅 QR_CODE。Android 包可见性仅声明这两个包，不请求全部软件列表。

扫码需要用户设备上已经安装支持此协议、可调用的摄像头扫码应用。本包未内置摄像头或图像识别 SDK，不新增 CAMERA 权限，不依赖 GMS 动态模块；因此离线能否拍摄扫码取决于该外部应用本身。没有支持应用、启动被拒绝或用户取消时，界面明确说明并保留手动输入入口，不自动下载安装软件，也不将任意隐式处理器当可信来源。

外部结果始终不可信：仅接受 QR_CODE 格式及最多 2048 字符，复用 HTTP/HTTPS 根地址规范，拒绝账号密码、路径、查询串、片段、危险 scheme 和不安全字符。HTTP 无证书前置条件；HTTPS 保持系统 CA、域名及证书验证，不安装信任所有证书的实现。默认端口分别归一为 80/443，协议或端口变化均按不同来源处理。有效结果只填入候选地址并提示点击“验证并连接”。服务产品身份/API/最低客户端版本探测、完整地址与版本确认、用户点击连接、清理旧 Cookie/缓存、Keystore 保存流程仍必须全部经过；扫码回调本身不会联网探测、绑定或登录。探针不跟随重定向、不带 WebView Cookie。扫码等待标记和候选在 Android 实例重建时恢复，其他 Activity 回调不处理。

原生网络策略显式允许明文 HTTP；WebView 仍只在配对来源内共享会话，HTTPS 页面不加载 HTTP 混合内容。HTTP 服务本身必须提供与其部署协议一致的 Cookie/代理配置，客户端不会通过关闭 HTTPS 校验模拟明文服务。CC 布局及资源复用依据见 reports/cc-browser-reference/REPORT.md。

## 返回顺序与页面清理

原生配对页面先返回原已配对服务；网页区域只在当前主文档为已配对同源时读取随 APK 打包的固定脚本，不暴露 JavaScript bridge，也不拼接网页消息或地址为代码。

脚本选择当前焦点所属的开放 dialog，或最后一个开放 dialog。先派发可取消的 cancel 事件：页面 preventDefault 时保留弹窗并消费此次返回；未阻止时调用 dialog.close，让原有 close 事件完成页面清理；页面在 cancel 中自行 close 时不重复 close。无开放 dialog 才执行一次 WebView 历史返回，没有历史才关闭 Activity。

每次异步返回分配单调代次；检查未完成时重复按返回不创建第二次历史请求。导航开始、失败、换服务、清理登录、Activity 销毁均撤销未完成检查。回调必须仍为本代、同一文档地址且 Activity/WebView 有效。750 ms 截止仅清除等待并提示页面未响应，不猜测无弹窗并后退；迟到回调不能再次后退或退出。

## 验证范围

新增 JVM 用例覆盖 HTTP/HTTPS 默认端口、IP/IPv6、扫码格式/长度/URL 边界、协议变化、HTTP 身份探测/重定向/错误身份/HTML 拒绝、异步重复返回、截止或取消后迟到回调、错误代次；原有 HTTPS 探测、未知证书拒绝与页面状态用例一起运行。

新增 tests/android/dialog-back.mjs 在真实 Chromium DOM 执行 APK 中完全相同的脚本，覆盖无弹窗、正常 cancel→close 清理、preventDefault、双弹窗只关闭顶层、cancel 中自行关闭、不占焦点的非模态弹窗。该测试是浏览器脚本语义验证，不能代替 Android WebView 实机返回、外部摄像头扫码、旋转回调或系统手势验收。

正式签名和授权设备缺失的状态保留，不将本包标为稳定发布。

## 冻结执行证据

证据目录 .runtime/android-pairing-navigation-20261027/；应用与测试源在构建开始后未修改。

| 命令/范围 | 资源与隔离 | 结果 |
| --- | --- | --- |
| bash android/build-candidate.sh | 工具链固定，CPU 1、内存 1536 MiB、Gradle worker 1；容器内复制冻结源码再编译 | 退出 0，2m32s，90 个任务；23 项 JVM/TLS 测试全通过，0 skipped；Lint 0 error/1 API 兼容性 warning；debug/unsigned APK 均成功 |
| Docker ai-football-browser:v2.0.0-dev 执行 tests/android/dialog-back.mjs | 只读冻结脚本，network none、CPU 0.5、内存 512 MiB、pids 256 | 退出 0，真实 Chromium 6 场景通过；日志 dialog-back.log |
| python -m unittest tests.android.test_release_policy tests.android.test_device_contract tests.test_release_manifest tests.test_stable_release_plan -v | 固定 Python 镜像、只读冻结副本、network none、CPU 0.25、内存 256 MiB | 退出 0，26 项通过；日志 contracts.log |
| 固定 Python 镜像核对构建源码 SHA-256、23 项 XML 和真实 aapt 权限输出 | network none、只读挂载、CPU 0.25、内存 256 MiB | 退出 0，源码无差异，权限精确为 INTERNET/ACCESS_NETWORK_STATE；日志 frozen-verification.log |

构建日志 build-candidate.log；产物及完整源摘要已独立复制到本证据目录 artifacts/，避免后续构建覆盖。源父提交 e29e1ec8884401a56d3175468e03ab2c83992a24；本独立包当时未提交，准确源由 source-sha256.txt 约束，其自身 SHA-256 为 13100aa1487e325c50628f43beb46273c5a29733ac62535506af037d3abbbbed。

debug APK SHA-256：82ab1c86dd8f1617c50ad3dd42d978cfacd00297fce7fd3034239a620363a092；unsigned APK SHA-256：480035316e853d2ca3bd2ce0386c69450d582c3d60c8fb4b329e7ae9d5909e8d。debug 为可验证 Android Debug 签名，unsigned 无正式签名。

发现并修复构建证据包装缺陷：重复构建前清理本项目受控产物目录中的 unit-results，再复制当前结果，避免旧 XML 留在根级、最新结果被嵌套导致误读。未删除任何历史业务数据；本次清理仅可重建测试产物。

测试准备曾错误假设两个 namespace 目录存在初始化模块，复制命令退出 1；确认本仓库采用 namespace 导入后移除该复制步骤，以相同测试命令重新执行退出 0。没有以失败准备步骤冒充验证成功。

仍需受控 Android 设备执行：实际外部扫码器拍摄/取消/缺失、旋转或进程重建回调、服务验证确认、WebView 同源弹窗返回与系统手势、横屏/大字体。只读设备探测此前授权设备为 0，不能将 JVM/Chromium 的通过代替该项验收。
