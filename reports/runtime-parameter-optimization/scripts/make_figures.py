#!/usr/bin/env python3
"""Plot the illustrative CI sensitivity CSV with required Chinese font."""
import csv
from pathlib import Path

CHINESE_FONT = Path('/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc')
PERCENT_SCALE = 100
FIGURE_WIDTH_IN = 8
FIGURE_HEIGHT_IN = 5
FIGURE_DPI = 150


def main():
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties

    if not CHINESE_FONT.is_file():
        raise FileNotFoundError('Chinese font must be mounted: ' + str(CHINESE_FONT))
    font = FontProperties(fname=str(CHINESE_FONT))
    plt.rcParams['axes.unicode_minus'] = False
    root = Path(__file__).resolve().parents[1]
    with (root / 'data/sensitivity.csv').open(encoding='utf-8') as stream:
        rows = [r for r in csv.DictReader(stream) if r['metric'] == 'accuracy_ci95_half_width']
    if not rows:
        raise ValueError('No independent-match CI sensitivity records')
    matches = [int(r['independent_matches']) for r in rows]
    values = [float(r['value']) * PERCENT_SCALE for r in rows]
    fig, ax = plt.subplots(figsize=(FIGURE_WIDTH_IN, FIGURE_HEIGHT_IN))
    ax.bar([str(v) for v in matches], values, color='#3388cc', width=0.55)
    for i, value in enumerate(values):
        ax.text(i, value + 0.3, f'±{value:.1f} pp', ha='center', fontproperties=font)
    ax.set_title('独立比赛样本量与正确率区间：量级示意', fontproperties=font, pad=16)
    ax.set_xlabel('独立比赛数量（不能用同场决策条目数替代）', fontproperties=font)
    ax.set_ylabel('95%区间近似半宽 / 百分点', fontproperties=font)
    ax.set_ylim(0, max(values) * 1.2)
    ax.spines[['top', 'right']].set_visible(False)
    ax.yaxis.grid(True, alpha=0.2)
    ax.set_axisbelow(True)
    fig.text(0.5, 0.02, '假设正确率50%；正态独立样本近似；非实际成绩；相关样本须按比赛聚类评估',
             ha='center', fontproperties=font, fontsize=9)
    fig.tight_layout(rect=(0, 0.055, 1, 1))
    target = root / 'images/independent-match-ci-sensitivity.png'
    fig.savefig(target, dpi=FIGURE_DPI)
    plt.close(fig)
    print(target)


if __name__ == '__main__':
    main()
