#!/usr/bin/env python3
"""Render four figures from the generated CSVs, in a container with WQY font."""
from __future__ import annotations

import csv
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
from matplotlib import font_manager, pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
FONT = Path('/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc')
DPI = 150
FIGURE_SIZE = (11, 4.5)
MAIN_COLOR = '#176b91'
NEGATIVE_COLOR = '#be4659'


def read(name: str) -> list[dict[str, str]]:
    with (ROOT / 'data' / name).open() as f:
        return list(csv.DictReader(f))


def save(fig: plt.Figure, name: str) -> None:
    fig.tight_layout()
    fig.savefig(ROOT / 'images' / name, dpi=DPI)
    plt.close(fig)


def main() -> None:
    if not FONT.is_file():
        raise RuntimeError('WQY Chinese font is required; mount the host font directory')
    font_manager.fontManager.addfont(str(FONT))
    plt.rcParams['font.family'] = font_manager.FontProperties(fname=str(FONT)).get_name()
    plt.rcParams['axes.unicode_minus'] = False
    plt.rcParams['axes.spines.top'] = False
    plt.rcParams['axes.spines.right'] = False
    rows = read('historical_by_market.csv')
    fig, ax = plt.subplots(figsize=FIGURE_SIZE)
    vals = [100 * float(r['roi_recomputed']) for r in rows]
    ax.bar([r['market'] for r in rows], vals, color=[MAIN_COLOR if v >= 0 else NEGATIVE_COLOR for v in vals])
    ax.axhline(0, color='#444', linewidth=0.7)
    ax.set_ylabel('推荐赔率本地重算 ROI（%）')
    ax.set_title('固定历史样本：分盘口损益；非场馆权威现金收益')
    for i, row in enumerate(rows):
        ax.text(i, vals[i], f"{vals[i]:.1f}%\n{row['orders']}注", ha='center', va='bottom' if vals[i] >= 0 else 'top')
    ax.set_ylim(min(vals) - 6, max(vals) + 6)
    save(fig, 'historical_markets.png')
    rows = read('public_league_profiles.csv')
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.5))
    specs = [('goals_per_match', 1, '场均总进球'), ('draw_rate', 100, '平局率（%）'),
             ('mean_overround', 100, 'B365 赛前过圆（%）')]
    for ax, (key, scale, title) in zip(axes, specs):
        ax.bar([r['league'] for r in rows], [float(r[key]) * scale for r in rows], color=MAIN_COLOR)
        ax.set_title(title)
        ax.set_ylim(bottom=0)
    fig.suptitle('公开 2024/25 赛季描述：存在差异；尚未验证可盈利或因果机制')
    save(fig, 'league_profiles.png')
    fig, axes = plt.subplots(1, 2, figsize=FIGURE_SIZE)
    rows = read('margin_identity.csv')
    axes[0].plot([float(r['overround']) * 100 for r in rows],
                 [float(r['same_quote_ev']) * 100 for r in rows], marker='o', color=NEGATIVE_COLOR)
    axes[0].set(xlabel='过圆 S-1（%）', ylabel='同价买回理论 EV（%）', title='比例去水不会自动创造优势')
    rows = read('probability_sensitivity.csv')
    for error in sorted({r['probability_error'] for r in rows}, key=float):
        subset = [r for r in rows if r['probability_error'] == error]
        axes[1].plot([float(r['decimal_odds']) for r in subset], [100 * float(r['ev_error']) for r in subset],
                     marker='o', label=f'概率误差 {100 * float(error):.0f} 个百分点')
    axes[1].set(xlabel='十进制赔率', ylabel='EV 误差（百分点）', title='理论演示：赔率放大概率误差')
    axes[1].legend(fontsize=8)
    save(fig, 'pricing_theory.png')
    fig, axes = plt.subplots(1, 2, figsize=FIGURE_SIZE)
    rows = read('pooling_sensitivity.csv')
    for prior in sorted({r['prior_strength'] for r in rows}, key=float):
        subset = [r for r in rows if r['prior_strength'] == prior]
        axes[0].plot([int(r['matches']) for r in subset], [float(r['league_weight']) for r in subset],
                     marker='o', label=f'演示 k={prior}')
    axes[0].set(xlabel='独立比赛数', ylabel='联赛参数权重 n/(n+k)', title='部分池化示意；k 未用数据拟合', xscale='log')
    axes[0].legend(fontsize=8)
    rows = read('tick_correlation.csv')
    axes[1].plot([float(r['assumed_within_match_correlation']) for r in rows],
                 [float(r['effective_observations_per_match']) for r in rows], marker='o', color=MAIN_COLOR)
    axes[1].set(xlabel='假设的同场相关系数', ylabel='40 条记录的等相关有效观测数', title='理论示意；不是实测有效样本量')
    save(fig, 'pooling_and_sampling.png')
    print('4 figures generated with WQY; theory and measurements labelled separately')


if __name__ == '__main__':
    main()
