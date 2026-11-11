# 视觉基线复核更新记录（R7，2026-11-11）

## 为什么更新

R7 期间 `web/app` 有三处已提交但此前未发布的视觉变更：

| 提交 | 内容 | 影响 |
| --- | --- | --- |
| `4bfcdf9f` | 用有文档的颜色令牌替换散落硬编码色值 | 页头、导航、卡片描边、状态色全部改变 |
| `e68cde9b` | 首屏多放比赛行（密度调整） | 行高、内边距、列表可见条数改变 |
| `6ede8538` | 免费方案不再渲染成坏掉的购买卡 | 会员方案卡价格/期限文案改变 |

这些变更在 `ecba9368` 之后提交，而视觉基线仍是 `ecba9368` 时的像素，因此
`sh tests/browser/visual-check.sh` 在 14 屏全部失败。失败是基线过期，不是回归：
逐屏上下对照（旧基线在上、当前渲染在下）内容、行数、数据与布局一致，只有配色和密度不同。

## 复核方式

1. 确认冻结样本未变：`tests/browser/visual-fixture.mjs` 的 sha256 仍为 `d19b7fbdad0d`，
   与 `manifest.json` 记录的 `fixture_sha256` 相同；浏览器版本 `141.0.7390.37`、时区
   `Asia/Shanghai`、固定时刻 `2026-10-26T11:11:00Z` 同样未变。因此差异只可能来自 `web/app`。
2. 逐屏生成 `基线 / 当前` 对照图并人工查看 14 屏（7 页面 × 1440×960 / 390×844）。
3. 只在确认差异为预期的令牌与密度变更后，才用显式审阅开关重建基线：
   `.runtime/visual-update.sh`（内部 `VISUAL_BASELINE_UPDATE=reviewed` + `--update`）。
4. 重建后 manifest 的 policy 字段与全部 screen 状态与旧 manifest 逐项比对，确认
   **只有像素内容变化，策略未被放宽**：`channel_threshold` 仍为 16，
   `max_changed_ratio` 仍为 0.002，屏幕数量仍为 14。
5. 用未修改的 `sh tests/browser/visual-check.sh`（比较模式）复跑，14/14 通过。

没有调高阈值，也没有删除或跳过任何屏幕。

## 更新前后的差异比例

`changed_ratio` 为更新时当前渲染相对旧基线的差异比例；`passed` 表示新基线自身比较为 0 差异。

| 屏幕 | 宽度 | 更新前 | 更新后 |
| --- | --- | --- | --- |
| scores | 1440 | 0.1679 | passed |
| schedule | 1440 | 0.1868 | passed |
| experts | 1440 | 0.1114 | passed |
| membership | 1440 | 0.0818 | passed |
| account-guest | 1440 | 0.0873 | passed |
| admin-guest | 1440 | 0.0756 | passed |
| match-detail | 1440 | 0.4981 | passed |
| scores | 390 | 0.2280 | passed |
| schedule | 390 | 0.2292 | passed |
| experts | 390 | 0.1467 | passed |
| membership | 390 | 0.0822 | passed |
| account-guest | 390 | 0.0901 | passed |
| admin-guest | 390 | 0.0840 | passed |
| match-detail | 390 | 0.2764 | passed |

`match-detail` 桌面端比例最高（0.4981），原因是比赛详情弹层覆盖了几乎整个视口，
弹层底色、表格描边和状态标签全部使用了新令牌，改动面积与弹层面积成正比。
已单独查看该屏：利物浦 1:0 曼城、系统当前盘口三条有效盘口与价格、专家推荐空态均正常。

## 专家跟随控件基线

`tests/browser/profitable-experts.mjs` 另有一套控件截图基线（`profitable-mode-1440/390`），
同样因令牌化描边而变化（1440 端 0.0177、390 端 0.1595），文案与布局一致。
用 `.runtime/profitable-update.sh` 以相同审阅流程重建，policy 字段同样逐项未变。

## 证据

| 项 | 位置 / 值 |
| --- | --- |
| 更新前失败结果 | `.runtime/visual-ui-3854730/visual-results.json`（status=failed） |
| 基线更新结果 | `.runtime/visual-ui-update-3870193/visual-results.json`（mode=baseline_update） |
| 控件基线更新 | `.runtime/profitable-update-3876632/profitable-browser-results.json` |
| 更新后比较 | `.runtime/visual-ui-3877552/visual-results.json`（status=passed，14 屏） |
| 执行环境 | 容器 `ai-football-browser:v2.0.0-dev`，CPU 0.25 / 内存 512 MB |

以上目录位于工作树 `.runtime/` 下，属于本地证据，不入库；入库的是基线像素与 manifest。

## 局限

- 基线比较使用冻结的合成样本，不是生产数据截图（manifest 中
  `synthetic_data: true`、`production_evidence: false`）。它证明渲染未被意外改变，
  不证明真实数据下的内容正确性；真实数据验收另见浏览器套件与发布记录。
- 基线只覆盖 7 个页面的游客态与两个自定义宽度。登录态、管理员登录后的页面
  仍由功能测试覆盖，未纳入像素基线。
