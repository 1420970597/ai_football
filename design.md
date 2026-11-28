# 球讯比分界面设计

球讯网页使用 Hallmark Sport 2.0.0 的配色、数字字体和赛果表规则。比分首页依据 Sport 模板示例的 Poster 和 Stat-Led 构图组织，业务页面共用同一套材质。

## 页面布局

- 比分使用全宽球场照片，其下为赛事日期、已加载场次、可见滚球数、更新时间、筛选和全宽赛事表。手机比赛行内两队竖向排列，比分在右侧。
- 专家页并排显示筛选和榜单，原文页以发言列表为主体。会员页以权益目录和套餐表为主体。
- 个人中心以身份说明和设置工作区并列。管理页面以独立身份和横向分区导航组织运维操作。
- 桌面使用顶部导航与页尾辅助入口。手机使用五项底栏。比赛、原文和审核详情复用同一套弹窗交互。

## 颜色与文字

- [Sport 变量](web/app/sport/tokens.css) 定义模板变量和间距比例。[网页样式](web/app/app.css) 定义业务状态与组件。
- 冷灰底、深墨色和浅绿色填充构成主色。强调填充配深墨文字。报价和订单状态仍有可读文本。
- 本地 Saira Condensed 800 用于大数字，正文与中文标题使用系统字体。时间和盘口使用等宽数字。
- 行间使用细分隔线，大区块使用 2px 边线。所有状态就地更新并支持减少动画设置。

## 来源

- [Hallmark Sport 模板](https://github.com/Nutlope/hallmark/blob/04977e15f3dfd6ee4b74c887dac3eb52665d6c92/skills/hallmark/references/themes/sport.md)
- [Sport 模板示例](https://github.com/Nutlope/hallmark/blob/04977e15f3dfd6ee4b74c887dac3eb52665d6c92/skills/hallmark/references/themes/sport.jpg)
- [球场照片的许可与项目证据](docs/platform-v2/HALLMARK_SPORT_REDESIGN_20261118.md)
