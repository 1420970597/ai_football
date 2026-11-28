"""Read-only personal history projections. No order submission or estimated P&L."""
from decimal import Decimal


def summarize(items, complete):
    groups = {}
    for item in items:
        match_id = item.get('match_id')
        if not match_id:
            continue
        group = groups.setdefault('leyu:' + match_id, {
            'fixture_id': 'leyu:' + match_id, 'bought': True, 'order_count': 0,
            'unsettled_count': 0, 'settled_count': 0, 'unknown_profit_count': 0,
            'currencies': {},
        })
        group['order_count'] += 1
        if item['state'] == 'unsettled':
            group['unsettled_count'] += 1
            continue
        group['settled_count'] += 1
        currency = item.get('currency') or 'unknown'
        money = group['currencies'].setdefault(currency, {
            'currency': item.get('currency'), 'settled_net': Decimal(0),
            'known_profit_count': 0, 'unknown_profit_count': 0, 'fees_unknown': False,
        })
        if item.get('currency') is None or item.get('net_profit') is None or item.get('pnl_basis') not in {
                'provider_reported_net', 'gross_return_minus_stake'}:
            money['unknown_profit_count'] += 1
            group['unknown_profit_count'] += 1
        else:
            money['settled_net'] += Decimal(str(item['net_profit']))
            money['known_profit_count'] += 1
        money['fees_unknown'] |= item.get('fees') is None
    for group in groups.values():
        group['currencies'] = [
            {**money, 'settled_net': str(money['settled_net']) if money['known_profit_count'] else None}
            for money in group['currencies'].values()]
    return {
        'matches': list(groups.values()),
        'association_complete': complete and all(item.get('match_id') for item in items),
        'order_count': len(items), 'match_count': len(groups),
        'unsettled_match_count': sum(g['unsettled_count'] > 0 for g in groups.values()),
        'settled_match_count': sum(g['settled_count'] > 0 for g in groups.values()),
        'unmapped_order_count': sum(not item.get('match_id') for item in items),
    }
