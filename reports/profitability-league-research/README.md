# 纯算法亏损、替换路线与联赛自动优化研究

2026-10-15；研究基线 `4a1c1cd68db6f2b12f8c836edc126b33de8c4b97`。

- [完整中文报告](REPORT.md) / [可浏览 HTML](REPORT.html)
- [历史重算与公开联赛描述](data/summary.json)
- [16 项来源及阅读边界](data/sources.csv)
- [输入快照与下载校验](data/input_manifest.json)
- [真实验证记录](data/validation.json) / [分析与规划共识](data/consensus.md)

本轮为研究交付。算法替换、联赛路由、盈利与真实执行效果尚未实现或验收；研究脚本不访问生产 PG、S3 或场馆，不产生真实订单。

复现描述统计（Python 标准库；公开源若修改会拒绝哈希不一致的内容）：

```bash
./scripts/cpu-limited.sh run -- python3 reports/profitability-league-research/scripts/analyze.py \
  --source-dir output/profitability-league-research/public --fetch
./scripts/cpu-limited.sh run -- python3 -m unittest discover \
  -s reports/profitability-league-research/scripts -p 'test_*.py' -v
```

图像需要容器及中文字体，不在宿主机安装依赖：

```bash
docker run --rm --cpus=2 --memory=2g --pids-limit=128 \
  -v "$PWD:/w" -v /usr/share/fonts/truetype/wqy:/usr/share/fonts/truetype/wqy:ro \
  -w /w python:3.12-slim sh -c \
  'pip install --no-cache-dir --disable-pip-version-check -q matplotlib==3.10.8 && python reports/profitability-league-research/scripts/make_figures.py'
python3 reports/profitability-league-research/scripts/md_to_html.py \
  reports/profitability-league-research/REPORT.md reports/profitability-league-research/REPORT.html
```

原始公开赛季 CSV 缓存在 `output/`，报告只提交聚合数据和来源哈希，避免复制大量运行产物。固定历史订单直接复用已入库的脱敏 TASK-58 附件。4 张图、6 份派生 CSV、1 份汇总 JSON 可重新生成；`sources.csv`、两个评估矩阵和清单属于人工维护研究材料。
