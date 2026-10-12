#!/usr/bin/env python3
"""Reproduce illustrative independent-match CI/Kelly sensitivity; no live changes."""
import csv
import math
from pathlib import Path

INDEPENDENT_MATCHES = (30, 100, 300)
WIN_PROBABILITY = 0.5
NORMAL_95_Z = 1.96
KELLY_FRACTIONS = (0.10, 0.15, 0.25)
DECIMAL_ODDS = (1.5, 2.0, 3.0)


def normal_95_half_width(matches, probability=WIN_PROBABILITY):
    if matches <= 0 or not 0 <= probability <= 1:
        raise ValueError('matches must be positive and probability between 0 and 1')
    return NORMAL_95_Z * math.sqrt(probability * (1 - probability) / matches)


def main():
    target = Path(__file__).resolve().parents[1] / 'data' / 'sensitivity.csv'
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(('metric', 'independent_matches', 'decimal_odds', 'kelly_fraction', 'value', 'basis'))
        for matches in INDEPENDENT_MATCHES:
            half_width = normal_95_half_width(matches)
            writer.writerow(('accuracy_ci95_half_width', matches, '', '', half_width, 'illustrative_normal_independent_match_approximation'))
        for odds in DECIMAL_ODDS:
            for fraction in KELLY_FRACTIONS:
                writer.writerow(('kelly_stake_probability_derivative', '', odds, fraction, fraction * odds / (odds - 1), 'binary_no_push_positive_edge_only'))
    print(str(target))


if __name__ == '__main__':
    main()
