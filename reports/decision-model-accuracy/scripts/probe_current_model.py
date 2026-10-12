#!/usr/bin/env python3
"""Small deterministic code probes; no training or production database writes."""
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT))

from core.learned_model import FEATURES, features, metrics  # noqa: E402


def main() -> None:
    row = {'market': 'OU', 'outcome': 'over', 'line': '2.5',
           'entry_score': [1, 0], 'entry_clock_s': 1200,
           'p_market': .5, 'p_fused': .52, 'training_trend_pct': 5,
           'algorithm': 'economic_ensemble',
           'training_history': [[0, 2], [1000, 1.9], [2000, 2.1]]}
    short = features(row)
    long = features({**row, 'training_history': [[0, 2], [300000, 1.9], [600000, 2.1]]})
    six = features({**row, 'entry_score': [6, 0]})
    ten = features({**row, 'entry_score': [10, 0]})
    rows = [{'match_id': 'illustrative-HAD', 'baseline': p, 'y': y}
            for p, y in zip((.4, .35, .25), (0, 0, 1))]
    result = {
        'type': 'deterministic_code_probe_not_backtest',
        'feature_count': len(FEATURES),
        'same_prices_2s_vs_600s_features_equal': short == long,
        'score_difference_6_vs_10_features_equal': six == ten,
        'had_example': {'probabilities': [.4, .35, .25], 'actual_outcome': 'away',
                        'top1_correct': False, 'per_direction_metrics': metrics(rows, None)},
    }
    target = Path(__file__).resolve().parents[1] / 'data' / 'code_probes.json'
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
