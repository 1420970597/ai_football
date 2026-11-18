# Hallmark Sport 模板重构

状态为设计已记录。开始于 2026-11-18，基于 `9cc145fc`。用户要求按技能模板重做页面布局。Wayfind 预览及已有功能测试提供重构前证据。

| 工作包 | 责任目录 | 依赖与缺口 | 实现 | 验收与失败处理 |
| --- | --- | --- | --- | --- |
| 比分和详情 | `web/app/` | 现有比分、用户和盘口数据契约。Wayfind 仍采用比分表与右栏并排 | 使用 Hallmark Sport 的 Poster 与 Stat-Led 示例，赛事为首操作，墙面式海报下接数字、日期和全宽赛果表 | 保留比分密度、比分与盘口关联、滚动焦点、深链和空/失败状态。出现不一致时保留证据重跑 |
| 专家、会员、个人与管理 | `web/app/` | 既有 API、角色和表单。需要重组版面 | 专家由榜单与筛选分栏，原文使用索引列表，会员用权益目录和方案表，个人分账户与交易，管理使用全宽工作区 | 对照字段、角色、授权、来源搜索、列表与详情。用 Docker 隔离数据验证 |
| 客户端与预览 | `android/`、`tests/browser/` | 已有 APK 工具链和 3018 只读预览。真机可用性待确认 | Android 跟随网页视觉规则，复验后以同端口替换预览 | 构建、双端视觉和互动、真实公开读取、切换失败恢复 |

选择 Sport 而不是延续 Wayfind。模板来源为 Hallmark `04977e15f3dfd6ee4b74c887dac3eb52665d6c92` 的 `sport.md`、`sport.jpg` 和 `04-stat-led.md`，Docker 容器生成脚手架。下一步实现新首屏、比赛行及其他页面。

## 2026-11-18 06:08 UTC，预览已切换

- 全宽海报下方使用赛事数字条与全宽赛果表，手机主客队竖排。专家榜由首页右栏移入专家页，原文、会员、账户、管理分别使用列表与筛选、方案与权益、身份与设置、横向管理分区。版式及模板变量见根目录设计说明。
- 球场照片 [Worldcup 2010 Soccer Stadium at night](https://commons.wikimedia.org/wiki/File:Worldcup_2010_Soccer_Stadium_at_night_-_panoramio.jpg) 由 ngrund 拍摄，采用 [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/)，网页有署名、许可和改动说明。源图裁剪为 1000×450 并改为蓝黑调，转换为约 25 KB 的 WebP。替代为可授权球场实拍或纯色背景。字体使用 Google Fonts Saira Condensed 800 拉丁子集 v12，保留 SIL OFL 1.1，中文和其他字符使用系统字体。
- Docker 的 `tests/browser/verify.mjs` 退出 0，67 项。增加手机长队名边界检查并修复继承定位引起的重叠。`verify.mjs --a11y` 退出 0，23 项。复测修正原文弹窗关闭等待和 sRGB 读取。真实 API `sh tests/browser/integration-check.sh` 退出 0，26 项，替身交易调用 0。
- `sh tests/browser/visual-check.sh` 退出 0，14 屏及双端账户模式通过，阈值沿用旧基线。Hallmark `sloplint --render --kept` 最终退出 0，比分和专家八宽度底线均无失败，11 条提示级发现。比分首屏已移除专家收益数字，该样本在专家视图继续显示。前后对照见 `.runtime/hallmark-sport/lint/.hallmark/compare.html`。
- `bash android/build-candidate.sh` 退出 0，28 项 JVM 测试、Lint 和双构建，真机安装验收仍待执行。网页镜像 `sha256:0404cbf139fb97ee932ee03ca98a4c382d5fbeeb06c4c5f3308b70e0afbe1f79` 打包后在预览容器启动。通过宿主 IPv4 验证首页与公开赛事接口。当前监听 `0.0.0.0:3018`，旧预览容器停止并保留恢复路径。正式运行端口未切换。
- 双端篮球比分、专家关联比赛和本人历史专项通过。实读预览的 `tests/browser/score-summary-anchor.mjs` 退出 0，桌面与手机各 50 场，盘口响应均为 200，可见行位移分别约 0.41px 与 0.09px，双方均保留焦点、列表顺序且写请求为 0。原脚本默认容器地址与范围不符合本轮环境，已用可配置预览地址和显式全部赛事复验。
