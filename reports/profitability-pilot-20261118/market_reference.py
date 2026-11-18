"""Offline two-sided market benchmark for already aligned half-goal totals."""

import json
import sys
from decimal import Decimal, InvalidOperation

from price_contract import decimal, price_contract


def price_from_references(document):
    line = decimal(document['line'])
    if (line <= 0 or line % 1 != Decimal('0.5')
            or document['direction'] not in {'over', 'under'}):
        raise ValueError('a positive half-goal total and an over/under direction are required')
    references = document['references']
    if not isinstance(references, dict) or len(references) < 2:
        raise ValueError('two independent reference sources are required')

    scenarios = {}
    for source, quote in references.items():
        over, under = (decimal(quote[key]) for key in ('over', 'under'))
        if min(over, under) <= 1:
            raise ValueError('decimal reference odds must exceed one')
        probability_over = under / (over + under)
        # Representative scores on either side suffice for a half-goal total.
        scenarios[source] = [
            {'home': int(line) + 1, 'away': 0, 'probability': probability_over},
            {'home': int(line), 'away': 0, 'probability': 1 - probability_over},
        ]

    return price_contract(document | {
        'contract': {'sport': 'football', 'phase': 'scheduled', 'kind': 'total',
                     'period': 'full_time', 'line': line, 'selection': document['direction'],
                     'score_basis': 'full_period'},
        'scenarios': scenarios,
    })


if __name__ == '__main__':
    try:
        document = json.load(sys.stdin, parse_float=Decimal)
        print(json.dumps(price_from_references(document), default=str))
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        sys.exit(f'Invalid research input: {exc}')
