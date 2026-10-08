#!/usr/bin/env python3
"""理论区间示意；不是系统正确率实测。"""
import csv
import math
from pathlib import Path

HYPOTHETICAL_RATE = 0.80
CONFIDENCE_Z = 1.959963984540054
SAMPLE_SIZES = (10, 30, 100, 300, 1000)
CHINESE_FONT = '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc'


def wilson(successes, n, z=CONFIDENCE_Z):
    if n <= 0 or not 0 <= successes <= n:
        raise ValueError('样本数或成功数非法')
    p = successes / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0, center - half), min(1, center + half)


def main():
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties
    root = Path(__file__).resolve().parents[1]
    font = FontProperties(fname=CHINESE_FONT)
    plt.rcParams['axes.unicode_minus'] = False
    rows = [(n, round(n * HYPOTHETICAL_RATE), *wilson(round(n * HYPOTHETICAL_RATE), n))
            for n in SAMPLE_SIZES]
    with (root / 'data/wilson.csv').open('w') as f:
        w = csv.writer(f)
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


if __name__ == '__main__':
    main()
