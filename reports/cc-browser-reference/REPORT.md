# CC 浏览器旧研究复用与 App 重构

更新日期：2026-10-28。状态：设计已记录、代码已写；Android、951项后端、网页专项与部署后12组真实点击通过。远端CI发现账户刷新竞态，另设确定性反例修复并复验，未声明完整发布验收。

## 来源与证据边界

来源为用户指定旧项目备份 `/root/ai_football-v2-backups/ai_football-preclean-20261026.tar` 内的 `ai_football/reports/cc-browser-apk-analysis/`：REPORT.md、offline 页面、images 及 data/asset_inventory.csv。源报告研究 `io.orinn.browser` 5.5.2 / 2552，识别 Flutter 外壳与 InAppWebView。旧截图是离线重建页，报告未提供运行时布局测量，不能声称还原真实 APK 的精确尺寸或一比一复刻。

本轮使用旧分析中蓝色顶栏、居中宽版比分内容、紧凑筛选/日期/赛事行、桌面右栏与手机五项固定底栏的设计。桌面内容宽度明确设为 1240px，顶部 56px，手机 54px；正文 14px、手机13px、赛事12px。小字主色由参考亮蓝调整为 #0868c5 以保持可读性，分隔线 #e0e7ee，页面背景 #f3f6f9。上述数值为本轮实现决策，不是 APK 实测值。

不引入旧 APK 彩票、充值投注、彩票榜单或品牌。比分、专家、会员、个人与独立管理页面继续使用现行业务接口；原生壳提供 HTTP/HTTPS 配对和网络/返回处理，网页提供自适应业务界面。它是既有 Android WebView 架构的重构，不声称改写为 Flutter 或完全原生客户端。

## 静态资源清单

用户明确要求使用旧项目 CC 静态资源。来源是第三方 APK 提取，未找到许可证明；本轮不将它们登记为原创或已获许可。两个无品牌通用图标用于开发候选，正式发行前需核实使用权或以原创替换。源文件原样保留，运行路径 `/assets/cc-reference/`。

| 原文件 | 用途 | 源版本 | SHA256 | 权利状态 / 替代方案 |
| --- | --- | --- | --- | --- |
| home_announcement_icon.png | 服务/错误提示前图标 | CC 5.5.2/2552 | eef1d1c46b5f196252e3c84949c7a5add411e97918a9c48b12a81074b415bc9c | 未核实；可替换原创提示图标 |
| home_match_vs_icon.png | 无比分的未开赛赛事 VS | CC 5.5.2/2552 | 13823abb3f43e1560ac5ac642641d2167251c220add402985ea88a3cc49fe310 | 未核实；可替换原创 VS 排版 |

已实际查看并排除：bg_match_football.webp 含竞彩品牌；home_hot_match_header_icon.png 文案为热门资讯、与本页语义不符。旧 logo、横幅及彩票素材均未进入产品包。图标只是展示资源，赛事/专家/统计不得从旧离线样本读取。

## 工作包与验收

- 目标：响应用户 App 重构及不强制 HTTPS 的要求，延续已经完成的 Telegram/Hermes/比分交互修复。
- 责任目录：网页、Android、客户端测试、报告及进度；依赖既有 API、Docker 工具链、旧备份只读资源。
- 当前证据/缺口：旧研究和资源已定位；缺 APK 真实布局测量、素材许可证明与受控 Android 设备。不得以浏览器或 JVM 测试代替设备验收。
- 设计决策：保留现行业务与同源权限机制；允许用户明确配对 HTTP，无证书前置条件；HTTPS 继续系统证书验证；两种协议均验证服务产品与 API 身份，不跟随跨来源重定向。
- 实现动作：统一蓝色网页/原生壳、1240px 桌面内容与双端密度；复用通用提示/VS；调整 URL/探针/网络策略、二维码及帮助；补 HTTP/HTTPS 与不安全输入反例。
- 验收场景：桌面1440/手机390所有状态页切换、日期、详情、专家、会员/登录、管理与 TG；图片自然尺寸与无横溢；HTTP 实际探针、错误服务、重定向、HTTPS 未信任证书拒绝；完整冻结回归与候选构建。
- 失败处理：保留首次失败，修根因后原命令重跑；视觉预期变更须人工查看再更新基线；设备、许可、上游权限分别保留缺证状态。

## 本轮证据

- `bash android/build-candidate.sh` 退出0：28项JVM测试（策略12、导航6、HTTP/TLS探针10），零失败/错误/跳过；Lint、debug与unsigned release构建通过，90项Gradle动作，耗时2分31秒。debug SHA256 `9ab6a94b35ddeb84e71fb0d83ae938e861f1dd84ff01f6414685c63335a227bd`；unsigned release SHA256 `ee5f0bafaa813c8d8e1e60bf933b4fdfc9a1127c772df669442db1c6a0bab095`。该次构建记录父提交4619390和dirty状态，不冒充精确干净提交或正式签名包。
- `sh tests/browser/check.sh`、`sh tests/browser/accessibility-check.sh`、`sh tests/browser/model-configuration-check.sh`、`sh tests/browser/match-markets-check.sh` 均退出0，分别保存至 `.runtime/browser-ui-3702110`、`.runtime/accessibility-ui-3704598`、`.runtime/model-configuration-ui-3714075`、`.runtime/match-markets-ui-3718295`。覆盖真实PNG加载、双端点击、已确认无障碍规则、模型配置流程及1207条盘口无漏无重。
- `sh tests/browser/visual-check.sh` 首次因旧紫色基线差异退出1；人工复核1440/390两张联系表内的14屏后，按显式reviewed流程更新蓝色基线，再跑原命令退出0，证据 `.runtime/visual-ui-3721549/visual-results.json`。比较阈值未变（通道16、变化比例0.002），不是原站一比一对照证据。
- `sh tests/browser/integration-check.sh` 退出0，证据 `.runtime/web-integration-3721988`；真实API/临时PostgreSQL覆盖17项原场景、身份矩阵及8项账户完整流程；替身核验/刷新/回执各1，交易调用0、失败0，不写生产假数据。
- `sh infra/android/check-device.sh` 退出2：授权设备0、无KVM，实际安装、扫码/WebView及离线操作仍未验收；浏览器与JVM成功不能覆盖该缺口。
- 完整后端首次运行在汇总前收到SIGTERM、退出143，日志保留；原入口重跑退出0，951项393.143秒，零失败/错误/跳过。冻结565份文件摘要与81a324f比较仅两份文档不同，产品及测试一致；机器报告如实保留父提交与脏树状态。
- 81a324f已推送并部署Web候选 `2.0.0-dev.cc.20261028.1`，1440/390各6组真实HTTP点击全部通过、退出0。包括蓝色布局及两图标真实加载、五比赛状态、日期/详情、专家、管理员六分区与四TG来源、会员/个人页面；无浏览器JS错误与横溢。证据 `.runtime/cc-app-20261028/live-clicks.json`，没有向生产写入测试用户或订单。
- GitHub精确分支40611386478的后端与Android通过，浏览器账户流程失败；这比本地成功更严格地暴露了账户导航和手动刷新竞态。修复与确定性回归独立记录于进度E104.1，不以此前12组只读点击代替账户写操作隔离验证。
