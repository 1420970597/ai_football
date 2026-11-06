"""Provider rules established by native ordinary-order settlement receipts.

This is a calculation policy, never a rewrite of the recommendation snapshot.
See reports/e330-leyu-app-rule-rea-20261105.md and the E392 acceptance record.
"""
import re


LEYU_PERIOD_TOTAL_RULE = 'leyu-football-period-total-e330-20261106'
LEYU_REMAINING_HANDICAP_RULE = 'leyu-football-fulltime-live-handicap-e448-20261106'


def settlement_rule(fixture, signal):
    """Select only native markets with verified ordinary-order receipts."""
    identity = str(fixture.get('id') or '')
    if (re.fullmatch(r'leyu:[0-9]+', identity) is None
            or fixture.get('sport') != 'football'
            or signal.get('phase') != 'live'
            or signal.get('score_basis') != 'unknown'):
        return None
    market = str(signal.get('market_id'))
    if signal.get('market_kind') == 'total':
        period = {'2': 'full_time', '18': 'first_half'}.get(market)
        reference, basis = LEYU_PERIOD_TOTAL_RULE, 'full_period'
    elif signal.get('market_kind') == 'handicap' and market == '4':
        period = 'full_time'
        reference, basis = LEYU_REMAINING_HANDICAP_RULE, 'remaining_period'
    else:
        return None
    if period is None or signal.get('period') != period:
        return None
    return {'reference': reference, 'score_basis': basis,
            'provider': 'leyu', 'market_id': market, 'period': period}
