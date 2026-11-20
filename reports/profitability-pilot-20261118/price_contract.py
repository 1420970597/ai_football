"""Offline pricing of football contracts using the platform's settlement rules."""

import json
import sys
from decimal import Decimal, InvalidOperation

OUTCOME_FRACTIONS = {
    'win': (Decimal(1), Decimal(0)),
    'half_win': (Decimal('0.5'), Decimal(0)),
    'push': (Decimal(0), Decimal(0)),
    'half_loss': (Decimal(0), Decimal('0.5')),
    'loss': (Decimal(0), Decimal(1)),
}


def decimal(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('nonfinite decimal')
    return result


def price_contract(document):
    from app.settlement import settle

    signal = document['contract']
    if (signal.get('sport') != 'football' or signal.get('phase') not in {'scheduled', 'live'}
            or signal.get('kind') not in {'total', 'handicap'}
            or signal.get('period') not in {'full_time', 'first_half', 'second_half'}
            or signal.get('score_basis') not in {'full_period', 'remaining_period'}
            or signal['phase'] == 'scheduled' and signal['score_basis'] != 'full_period'):
        raise ValueError('unsupported or ambiguous football contract')
    scenarios = document['scenarios']
    if not isinstance(scenarios, dict) or not scenarios:
        raise ValueError('at least one score distribution is required')
    cost = decimal(document['cost_per_unit'])
    margin = decimal(document['required_net_edge'])
    offered = decimal(document['offered_odds'])
    if cost < 0 or margin < 0 or offered <= 1:
        raise ValueError('invalid cost, edge or odds')

    priced = {}
    for name, distribution in scenarios.items():
        mass = win = loss = Decimal(0)
        for row in distribution:
            probability = decimal(row['probability'])
            if not 0 < probability <= 1:
                raise ValueError('score probability must be in (0, 1]')
            result = {key: row.get(key) for key in ('home', 'away', 'half_home', 'half_away')}
            outcome = settle(dict(signal, price=2), dict(result, status='confirmed'))
            if outcome.outcome not in OUTCOME_FRACTIONS:
                raise ValueError(outcome.reason)
            winning, losing = OUTCOME_FRACTIONS[outcome.outcome]
            mass += probability
            win += probability * winning
            loss += probability * losing
        if mass != 1:
            raise ValueError('score probabilities must sum to one')
        priced[name] = {
            'expected_win_fraction': win,
            'expected_loss_fraction': loss,
            'net_at_offered_odds': win * (offered - 1) - loss - cost,
            'minimum_odds': 1 + (loss + cost + margin) / win if win else None,
        }

    floors = [row['minimum_odds'] for row in priced.values()]
    worst_net = min(row['net_at_offered_odds'] for row in priced.values())
    has_floor = all(floor is not None for floor in floors)
    return {
        'minimum_odds': max(floors) if has_floor else None,
        'worst_net_at_offered_odds': worst_net,
        'passes_price_test': has_floor and worst_net >= margin,
        'scenarios': priced,
    }


if __name__ == '__main__':
    try:
        document = json.load(sys.stdin, parse_float=Decimal)
        print(json.dumps(price_contract(document), default=str))
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        sys.exit(f'Invalid research input: {exc}')
