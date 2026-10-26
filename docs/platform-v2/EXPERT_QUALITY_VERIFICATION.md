# 专家推荐离线质量回归

更新时间：2026-10-26。阶段：合成结构化样本隔离验证通过。实现位于 `infra/quality/`，不读取数据库，不调用模型、Telegram、乐鱼或投注服务。

## 格式与证据

标签格式为 `football-review-labels-v1`，输出格式为 `football-review-predictions-v1`。标签集、运行记录及每条标签均须明确 `synthetic=true`；来源级别只接受 `synthetic`，来源引用以 `synthetic:` 开头。当前没有真实TG人工标注集，工具拒绝把未标记或真实来源记录混进本次合成回归。

标签集保存 `dataset_id`、`label_schema_version=1`、`annotation_version`、带时区的创建时间及 `samples`。每条样本保存稳定编号、`text/image/mixed` 类型、消息版本、内容SHA-256、按顺序排列的媒体SHA-256，以及人工预期：

```json
{
  "sample_id": "synthetic-example",
  "synthetic": true,
  "modality": "text",
  "source": {
    "level": "synthetic",
    "reference": "synthetic:example",
    "content_sha256": "64位小写十六进制摘要",
    "message_revision": 1,
    "media_sha256": []
  },
  "expected": {
    "decision": "accept",
    "recommendations": [{
      "intent_id": "synthetic-intent-one",
      "fixture_id": "synthetic-fixture-one",
      "market_id": "synthetic-total",
      "market_kind": "total",
      "period": "full_time",
      "line": "2.5",
      "selection": "over",
      "phase": "live"
    }]
  }
}
```

上例摘要文字仅作格式说明；可直接执行的合法样本在 `infra/quality/samples/`。拒绝标签必须使用空推荐数组和非空 `rejection_reason`，不能用缺少标签表示拒绝。图像/混合样本必须具备媒体哈希；没有原图的真实标签不能借此声称视觉质量已验证。

运行记录保存同一 `dataset_id`、独立 `run_id` 及 `provenance`：模型名称、模型版本、提示SHA-256、技能版本、Hermes版本、工具契约版本和生成时间。每条输出保存同一 `sample_id`、消息内容哈希、消息版本、按序媒体哈希、`status=completed/error` 和结构化 `accepted`。推荐绑定字段与标签一致，可附 `signal_id` 和系统去重确认 `duplicate`；模型自己声称的 `intent_id` 不被用来匹配人工标签。

缺少模型/提示/技能版本等信息时，报告列出 `provenance_missing`，不补成默认版本。缺消息、缺结果数组、缺状态、消息版本不符、内容哈希或媒体数量/顺序不符分别记录，不能算正确拒绝。标签版本和规范化标签摘要同时进入报告，方便复查两版本是否使用同一组标签。

## 彼此独立的指标

没有总分、自动优胜版本或自动晋级。报告分别给出：

- 输出数量代理：人工预期项数、去重后已接受项数、同数量消息、按数量计算的漏项/多项。仅有accepted时不能观察模型在调用工具前的内部抽取过程，该指标不能称为语义抽取准确率。
- 精确绑定：完整七维一致的推荐、漏掉的预期推荐和误接受推荐。比赛、原生玩法、玩法类型、期间、数值盘口线、方向、赛前/滚球阶段分别计正确、错误、缺失、对齐多解及未评估。
- 一条消息多推荐：完整识别、部分识别、无精确识别、数量漏项和精确绑定漏项。重复输出只算一个推荐，不能抵消第二项漏识别。
- 拒绝：预期可接受与应拒绝消息、负例误接受、正确拒绝、带原因的正确拒绝、无原因拒绝、正例被拒绝及未评估负例。框架报错且无accepted不算正确拒绝；报错前已经accepted的信号仍计入接受风险。
- 覆盖与重复：缺输出、错误、缺状态、来源未绑定的已接受项、重复输出、系统重复确认、孤立输出记录。没有人工标签的输出单独列出，不能默默删除。

每维 `correct_per_expected` 的分母包含全部预期项，缺失、对齐多解和未评估仍在分母中，不能通过丢记录提高比例。零分母返回null。负例误接受消息和所有误接受推荐分别命名，避免把错误比赛/盘口误认为安全接受。

盘口线使用Decimal规则规范化，`2.5`和`2.5000`相等；非法值、无穷值或复合字符串不自动转换为零。两条不同绑定使用同一signal编号直接拒绝；标签本身的重复推荐或重复样本编号也拒绝。

先匹配无缺失的完整七维身份，再对其余项作一对一最小字段差异诊断。数组顺序不决定对应关系。等价最优对应存在多解时标记ambiguous，保守地不把相关字段计为正确。该诊断不证明模型内部理解过程；复杂实现另以独立穷举对照验证最小代价和多解识别。

按文字/图像/混合及赛前/滚球/混合/负例分组，组内仍保留全部指标。两版本比较保存前后版本信息、每项指标增减及逐消息人工复核清单；汇总不变也会保留局部退化。

## 隔离命令与结果

```sh
docker run --rm --network none --cpus=0.5 --memory=256m \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -v /root/ai_football:/w:ro -w /w \
  ai-football-verifier:v2.0.0-dev \
  python -m unittest discover -s tests -p test_quality.py -v
```

最终 **20项通过，0.803秒，退出码0**。测试容器禁网，不需要模型密钥或真实账号。覆盖七维单独错误、双推荐倒序、重复不能补足漏项、数值规范化、最优对应多解、来源版本/图片清单、缺结果、报错后已接受风险、无标签输出、版本缺失、标签冲突、路径范围及输出不覆盖既有标签。

实际比较命令：

```sh
docker run --rm --user 0 --network none --cpus=0.5 --memory=256m \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -v /root/ai_football:/w:ro \
  -v /root/ai_football/reports/quality:/w/reports/quality:rw -w /w \
  ai-football-verifier:v2.0.0-dev \
  python -m infra.quality.cli compare \
    --labels infra/quality/samples/labels.json \
    --baseline infra/quality/samples/baseline.json \
    --candidate infra/quality/samples/candidate.json \
    --output reports/quality/synthetic-comparison.json
```

独立版本报告使用 `evaluate --labels … --run … --output …`。三个命令均实际退出码0。项目文件只读，只有报告子目录允许写入；报告以0600新建，已存在则失败，不覆盖。输入/输出必须位于项目内，单输入最多16 MiB、1000样本，每条最多32个去重推荐、64条原始输出，总预期项上限10000；超限报错，不截断后声称完整评估。

六条合成消息有五个预期推荐。实际报告对照：

| 指标 | 合成基线 | 合成候选 |
| --- | ---: | ---: |
| 精确接受项 | 2 | 5 |
| 误接受项 | 2 | 0 |
| 已评估预期项中的漏项 | 2 | 0 |
| 未评估预期项 | 1 | 0 |
| 重复输出 | 1 | 0 |
| 框架执行失败消息 | 0 | 1 |
| 正确拒绝消息 | 1 | 1 |

正确拒绝总数相同，但图像负例由正确拒绝退化为执行错误；另一负例改善不能抵消这个问题。比较报告仍把此图像消息列入人工复核，共六条变化。此表是固定测试样本行为，不是模型真实准确率、收益或盈利结论。

产物及SHA-256：

| 报告 | SHA-256 |
| --- | --- |
| `reports/quality/synthetic-baseline.json` | `2865254b2bcd90839676091902f99eff0c2e816120ddc4b58654da4339ab3225` |
| `reports/quality/synthetic-candidate.json` | `adcae5cadc6ce0dd5bd023f7996e78002c67b1446252ae746a32b34e9672faa4` |
| `reports/quality/synthetic-comparison.json` | `3ac4c923bc3cc2e0519d0feef92cb13425c8ad4ca23bdd9b6f77bcb75b31abf7` |

## 尚未验证

真实TG消息与授权图片、独立人工标注与复核分歧、实际模型输入输出、真实工具证据快照及模型提供方回归仍缺。当前合成来源哈希只用于测试绑定，不包含真实图像理解能力证据。正式评估前需获得许可范围内的真实样本、分层抽样、消息/专家隔离的训练测试划分及人工复核流程，再扩展真实来源格式；不得把当前报告当作真实质量或盈利证明。
