#!/usr/bin/env python3
"""Reproduce audit measurements and explicitly theoretical decision illustrations."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FONT = Path('/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc')
FIGURE_DPI = 170
CONFIDENCE_Z = 1.96
ILLUSTRATIVE_WIN_RATE = 0.60
PRECISION_SAMPLES = (100, 500, 1000, 2500, 5000, 10000)
EXAMPLE_BETS = (('高胜率低赔率', 0.78, 1.20), ('低胜率高赔率', 0.52, 2.05))
PROBABILITY_ERRORS = (0.01, 0.02, 0.05)
ILLUSTRATIVE_ODDS = (1.50, 2.00, 3.00)


def expected_return(probability: float, odds: float, paid_stake: float = 1.0) -> float:
    """Expected profit / original stake for a conditional paid-stake win probability."""
    if not all(math.isfinite(v) for v in (probability, odds, paid_stake)):
        raise ValueError('non-finite parameter')
    if not 0 <= probability <= 1 or odds <= 1 or not 0 <= paid_stake <= 1:
        raise ValueError('invalid probability, odds or paid stake')
    return paid_stake * (probability * odds - 1)


def iid_half_width(probability: float, matches: int) -> float:
    """Normal-approximation illustration; not a confidence interval for project data."""
    if not math.isfinite(probability) or not 0 <= probability <= 1 or matches <= 0:
        raise ValueError('invalid probability or sample count')
    return CONFIDENCE_Z * math.sqrt(probability * (1 - probability) / matches)


def write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError('empty chart data')
    with path.open('w', encoding='utf-8', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=fields, lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    # Import graphics only in Docker; arithmetic checks need only the stdlib.
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties

    if not FONT.exists():
        raise FileNotFoundError('Mount WenQuanYi font before generating Chinese charts')
    plt.rcParams['font.family'] = FontProperties(fname=str(FONT)).get_name()
    plt.rcParams['axes.unicode_minus'] = False
    plt.rcParams['figure.dpi'] = FIGURE_DPI
    plt.rcParams['axes.spines.top'] = False
    plt.rcParams['axes.spines.right'] = False
    image_root, data_root = ROOT / 'images', ROOT / 'data'
    image_root.mkdir(exist_ok=True)

    audit = json.loads((data_root / 'historical_audit.json').read_text())['validation']
    groups = (
        ('旧标签 / 旧模型', 'original_common'),
        ('修正标签 / 旧模型', 'corrected_targets_same_features'),
        ('修正标签 / 市场基准', 'corrected_baseline'),
    )
    audit_rows = [{'cohort': label, **audit[key]} for label, key in groups]
    write_csv(data_root / 'audit_comparison.csv',
              ('cohort', 'matches', 'decisions', 'accuracy', 'brier', 'log_loss'), audit_rows)
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.4))
    colors = ['#71869b', '#d7645d', '#38948b']
    labels = [r['cohort'].replace(' / ', '\n') for r in audit_rows]
    for ax, metric, title in zip(axes, ('accuracy', 'brier'), ('准确率（旧评估权重）', 'Brier（越低越好）')):
        values = [r[metric] * (100 if metric == 'accuracy' else 1) for r in audit_rows]
        ax.bar(labels, values, color=colors, width=0.6)
        ax.set_title(title)
        ax.set_ylim(0, max(values) * 1.17)
        for i, value in enumerate(values):
            ax.text(i, value, f'{value:.2f}%' if metric == 'accuracy' else f'{value:.4f}', ha='center', va='bottom')
    fig.suptitle('历史审计：同一139场 / 145,897条共同记录；不是干净重训成绩')
    fig.tight_layout()
    fig.savefig(image_root / 'historical_audit.png')
    plt.close(fig)

    ev_rows: list[dict[str, Any]] = [{'scenario': name, 'probability': p, 'odds': odds,
                'expected_return': expected_return(p, odds), 'evidence': 'theoretical'}
               for name, p, odds in EXAMPLE_BETS]
    write_csv(data_root / 'accuracy_ev_example.csv',
              ('scenario', 'probability', 'odds', 'expected_return', 'evidence'), ev_rows)
    sensitivity_rows = [{'odds': odds, 'probability_error': error,
                         'ev_error': odds * error, 'evidence': 'theoretical'}
                        for odds in ILLUSTRATIVE_ODDS for error in PROBABILITY_ERRORS]
    write_csv(data_root / 'ev_sensitivity.csv',
              ('odds', 'probability_error', 'ev_error', 'evidence'), sensitivity_rows)
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
    axes[0].bar([r['scenario'] for r in ev_rows], [r['expected_return'] * 100 for r in ev_rows], color=colors[1:])
    axes[0].axhline(0, color='#29394b', linewidth=0.8)
    axes[0].set_ylabel('理论期望收益 / 本金（%）')
    for i, row in enumerate(ev_rows):
        value = row['expected_return'] * 100
        axes[0].text(i, value, f"胜率{row['probability']:.0%} / 赔率{row['odds']:.2f}\nEV {value:+.1f}%", ha='center', va='bottom' if value > 0 else 'top')
    axes[0].set_ylim(-12, 12)
    for odds in ILLUSTRATIVE_ODDS:
        axes[1].plot([e * 100 for e in PROBABILITY_ERRORS],
                     [odds * e * 100 for e in PROBABILITY_ERRORS], marker='o', label=f'赔率 {odds:.2f}')
    axes[1].set_xlabel('胜率估计偏高（百分点）')
    axes[1].set_ylabel('EV估计偏高（本金百分点）')
    axes[1].legend()
    fig.suptitle('公式示意：准确率、赔率与概率误差；不是项目回测收益')
    fig.tight_layout()
    fig.savefig(image_root / 'accuracy_and_ev.png')
    plt.close(fig)

    precision_rows: list[dict[str, Any]] = [{'independent_matches': n, 'assumed_win_rate': ILLUSTRATIVE_WIN_RATE,
                       'iid_normal_half_width': iid_half_width(ILLUSTRATIVE_WIN_RATE, n),
                       'evidence': 'theoretical_IID_approximation'} for n in PRECISION_SAMPLES]
    write_csv(data_root / 'sample_precision.csv',
              ('independent_matches', 'assumed_win_rate', 'iid_normal_half_width', 'evidence'), precision_rows)
    fig, ax = plt.subplots(figsize=(8, 4.2))
    ax.plot(PRECISION_SAMPLES, [r['iid_normal_half_width'] * 100 for r in precision_rows], marker='o', color=colors[2])
    ax.set_xscale('log')
    ax.set_xlabel('独立比赛数量（示意）')
    ax.set_ylabel('95%正态近似半宽（百分点）')
    ax.set_title('假设胜率60%的IID示意；真实评估须按比赛/比赛日聚类')
    fig.tight_layout()
    fig.savefig(image_root / 'sample_precision.png')
    plt.close(fig)
    print('Generated 3 figures / 4 quantitative CSVs')


if __name__ == '__main__':
    main()
