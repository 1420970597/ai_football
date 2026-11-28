# 盘口级源时间延迟指标

更新：2026-10-27。补充 `QUOTE_SOURCE_AGE_WINDOWS.md` 中已确认的事件钟／报价钟指标歧义。本轮只修改 collector 实时接收指标、独立测试和本文；不改盘口解析、源时间、15 秒门槛、API 白名单、UI、配置或部署。

## 指标契约

现有事件级 `source_lag_*`、`queue_wait_*`、`delivery_age_*` 完整保留。新增两组标量，彼此各自保存最多 256 条盘口观测，退出进程即可丢弃，不新增数据库／本地历史文件：

| 前缀 | 起点与终点 | 观测范围 |
| --- | --- | --- |
| `quote_source_lag` | 盘口自身 source_updated_at → 盘口 received_at；盘口接收时间缺失才使用已有事件 received_at | 每个已解析 incoming 盘口，包括之后可能队列溢出的观测 |
| `quote_delivery_age` | 盘口自身 source_updated_at → 当前批次 on_batch 成功返回的同一完成时刻 | 现有事件去重后的成功回调批次；失败期间不提前记录、重试成功只记录一次 |

两个前缀分别提供以下后缀，可由主线纳入运维 API 白名单：

- `_ms_p50`、`_ms_p95`：有效样本的毫秒分位，按现有确定性 nearest-lower 算法；没有有效样本为 null。
- `_samples`：进入分位的非未来、非超七天样本数。
- `_window_size`：最近盘口观测总数，上限 256；未知值也占窗口，避免分位永远保留很早的有效值。
- `_known_count`：可解析且带时区的真实源时间数量，包含另行分类的 future 与 too_old。
- `_unknown_count`：源时间 null 或空文本；不以外层事件时间或接收时间替换。
- `_invalid_count`：无法解析、无时区、或接收参考时间无效。
- `_future_count`：有效源时间晚于参考时间；不进分位。
- `_too_old_count`：有效源时间早于参考时间超过七天；仍为 known，不进分位。真实三十天旧源会计入 known 与 too_old，绝不会伪称 unknown。

分类关系：`window_size = known_count + unknown_count + invalid_count`；`samples = known_count - future_count - too_old_count`。这些是诊断统计，不构成信号新鲜、有效报价、逐盘口数据库接受或可投注的承诺。on_batch 成功仍须结合入库 accepted 等业务证据判断。

## 验收证据

九个独立场景覆盖外层新事件时间与内层旧盘口时间不同、多个盘口独立计数、未知／无效／未来／三十天旧源、盘口自己的接收时间优先、两个窗口各 256 上限、未知新窗口清空陈旧分位、成功批次同一终点、失败回调不提前统计且重试只计一次、重复 incoming 与去重 delivery 数量差异。测试确认事件及盘口输入对象没有被修改。

冻结源码复制至 `.runtime/quote-latency-20261027/source` 后执行：

```sh
docker run --rm --network none --cpus .5 --memory 256m \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/work \
  -v /root/ai_football/.runtime/quote-latency-20261027/source:/work:ro -w /work \
  ai-football-verifier:v2.0.0-dev python -m unittest -v \
  tests.test_quote_latency_metrics tests.test_leyu_realtime \
  tests.test_runtime_quote_identity tests.test_leyu_snapshot_monotonic
```

最终 **41 项通过，失败／错误／跳过均 0，2.089 秒，退出码 0**。首轮 40 项亦通过，增加 delivery 窗口裁剪与旧源分类场景后执行完整同组最终验证；未通过修改生产门槛掩盖失败。日志为 `.runtime/quote-latency-20261027/test-final.log`。

冻结 SHA-256：实时模块 `e9a68844fd125e2dc49c366e2461f0032dc60422d7ce3de82529147e5f620195`；独立测试 `188587ffe035416aa4f69ed8c507a11e9988db9043168d0b172e842b481c7487`。

当前状态：代码及隔离定向验证通过并冻结；API 白名单与页面由主线接入，尚未声明运行中已发布或据此解决所有盘口陈旧问题。没有额外上游调用、续期、账户或资金操作。
