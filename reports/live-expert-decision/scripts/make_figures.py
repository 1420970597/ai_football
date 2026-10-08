#!/usr/bin/env python3
"""理论区间示意；不是系统正确率实测。"""
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from core.replay import CONFIDENCE_Z, wilson

HYPOTHETICAL_RATE = 0.80
SAMPLE_SIZES = (10, 30, 100, 300, 1000)
CHINESE_FONT = '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc'


def main():
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties
    root = Path(__file__).resolve().parents[1]
    font = FontProperties(fname=CHINESE_FONT)
    plt.rcParams['axes.unicode_minus'] = False
    rows = [(n, round(n * HYPOTHETICAL_RATE), *wilson(round(n * HYPOTHETICAL_RATE), n))
            for n in SAMPLE_SIZES]
    with (root / 'data/wilson.csv').open('w') as f:
        w = csv.writer(f, lineterminator='\n')
        w.writerow(('samples', 'successes', 'lower', 'upper', 'hypothetical_rate', 'z'))
        for row in rows:
            w.writerow((*row, HYPOTHETICAL_RATE, CONFIDENCE_Z))
    fig, ax = plt.subplots(figsize=(8, 4))
    for i, (n, k, low, high) in enumerate(rows):
        ax.plot((low, high), (i, i), color='#1c88ff', linewidth=5)
        ax.plot(k / n, i, 'o', color='#12304d')
    ax.set_yticks(range(len(rows)), [str(r[0]) for r in rows])
    ax.set_xlabel('假设观测命中率与95%区间（理论示意）', fontproperties=font)
    ax.set_ylabel('独立样本数', fontproperties=font)
    ax.set_title('80%观测命中率不等于80%可信下界', fontproperties=font)
    ax.set_xlim(0, 1)
    ax.grid(alpha=.2)
    fig.tight_layout()
    fig.savefig(root / 'images/wilson.png', dpi=160)
    plt.close(fig)
    summary = json.loads((root / 'data/public-summary.json').read_text())
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    minutes = [int(m) for m in summary['by_minute']]
    values = list(summary['by_minute'].values())
    axes[0].plot(minutes, [v['brier'] for v in values], 'o-', color='#1c88ff', label='比分与剩余时间基线')
    axes[0].plot(minutes, [v['baseline_brier'] for v in values], '--', color='#9ca6b5', label='无赛况赛前基线')
    axes[0].set_title('64场时间留出验证：Brier越低越好', fontproperties=font)
    axes[0].set_xlabel('决策时的比赛分钟', fontproperties=font)
    axes[0].legend(prop=font)
    reliability = summary['reliability']
    axes[1].plot([0, 1], [0, 1], '--', color='#9ca6b5')
    axes[1].plot([r['mean_probability'] for r in reliability], [r['accuracy'] for r in reliability], 'o-', color='#1c88ff')
    axes[1].set_title('概率校准：384个检查点，64场比赛', fontproperties=font)
    axes[1].set_xlabel('模型预测概率', fontproperties=font)
    axes[1].set_ylabel('实际方向命中率', fontproperties=font)
    for ax in axes:
        ax.grid(alpha=.2)
    fig.tight_layout()
    fig.savefig(root / 'images/public-replay.png', dpi=160)
    plt.close(fig)


if __name__ == '__main__':
    main()
