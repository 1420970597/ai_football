"""Compare the combined payout of verified football orders under supplied scores."""

from decimal import Decimal, InvalidOperation
import json
import sys

from app.settlement import settle
from price_contract import decimal


def compare_match_exposure(document):
    fixture_id = document['fixture_id']
    orders = document['orders']
    results = document['results']
    if not isinstance(fixture_id, str) or not fixture_id.strip() or not orders or not results:
        raise ValueError('one fixture, at least one order and one result are required')
    if not isinstance(orders, list) or not isinstance(results, list):
        raise ValueError('orders and results must be arrays')

    terms = []
    for order in orders:
        contract = order['contract']
        if (contract['fixture_id'] != fixture_id or contract['sport'] != 'football'
                or contract['phase'] not in {'scheduled', 'live'}
                or contract['score_basis'] not in {'full_period', 'remaining_period'}
                or contract['phase'] == 'scheduled' and contract['score_basis'] != 'full_period'):
            raise ValueError('every order must have verified terms on this fixture')
        stake = decimal(order['stake'])
        cost = decimal(order['cost'])
        if stake <= 0 or cost < 0:
            raise ValueError('each order requires positive stake and nonnegative cost')
        terms.append((contract | {'price': decimal(order['filled_price'])}, stake, cost))

    scenarios = []
    for result in results:
        if result.get('status') not in {'confirmed', 'void'}:
            raise ValueError('each scenario requires a definite result status')
        payouts = [settle(contract, result) for contract, _, _ in terms]
        for payout in payouts:
            if payout.unit_net is None:
                raise ValueError(payout.reason)
        net = sum(payout.unit_net * stake - cost for payout, (_, stake, cost) in zip(payouts, terms))
        scenarios.append({'result': result, 'net': net, 'outcomes': [payout.outcome for payout in payouts]})

    return {'fixture_id': fixture_id, 'total_stake': sum(stake for _, stake, _ in terms),
            'maximum_loss_in_supplied_results': max(Decimal(0), -min(item['net'] for item in scenarios)),
            'scenarios': scenarios}


if __name__ == '__main__':
    try:
        document = json.load(sys.stdin, parse_float=Decimal)
        print(json.dumps(compare_match_exposure(document), default=str))
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        sys.exit(f'Invalid exposure input: {exc}')
