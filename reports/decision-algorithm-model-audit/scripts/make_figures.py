#!/usr/bin/env python3
"""Reproduce audit inclusion counts; these bars do not measure predictive skill."""
from __future__ import annotations

import csv
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties

ROOT = Path(__file__).resolve().parents[1]
FONT = Path('/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc')
FIGURE_SIZE = (9, 5)
DPI = 150
BAR_WIDTH = 0.32
COLORS = ('#2463a0', '#b3bdc9')


def main() -> None:
    if not FONT.is_file():
        raise FileNotFoundError('Chinese font mount is required: ' + str(FONT))
    with (ROOT / 'data/audit_summary.csv').open(newline='', encoding='utf-8') as stream:
        counts = {(r['track'], r['category']): int(r['papers']) for r in csv.DictReader(stream)}
    if not counts or any(value < 0 for value in counts.values()):
        raise ValueError('Invalid or empty audit counts')
    font = FontProperties(fname=str(FONT))
    plt.rcParams['axes.unicode_minus'] = False
    fig, ax = plt.subplots(figsize=FIGURE_SIZE)
    tracks = ('algorithm', 'model')
    for offset, (category, label, color) in enumerate(
            zip(('included', 'not_included'), ('纳入独立论文', '辅助或排除记录'), COLORS)):
        bars = ax.bar([i + (offset - .5) * BAR_WIDTH for i in range(len(tracks))],
                      [counts[(track, category)] for track in tracks],
                      width=BAR_WIDTH, label=label, color=color)
        ax.bar_label(bars, padding=3)
    ax.set_xticks(range(len(tracks)), ('决策算法', '决策模型'), fontproperties=font)
    ax.set_ylabel('篇 / 条', fontproperties=font)
    ax.set_title('逐篇审计：纳入论文与排除记录（不是准确率）', fontproperties=font)
    ax.legend(prop=font)
    ax.spines[['top', 'right']].set_visible(False)
    fig.tight_layout()
    output = ROOT / 'images'
    output.mkdir(exist_ok=True)
    fig.savefig(output / 'audit_counts.png', dpi=DPI)
    plt.close(fig)
    print('Generated images/audit_counts.png from data/audit_summary.csv')


if __name__ == '__main__':
    main()
