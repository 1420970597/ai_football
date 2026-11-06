"""Provider rules established by native ordinary-order settlement receipts.

This is a calculation policy, never a rewrite of the recommendation snapshot.
See reports/e330-leyu-app-rule-rea-20261105.md and the E392 acceptance record.
"""
import re


LEYU_PERIOD_TOTAL_RULE = 'leyu-football-period-total-e330-20261106'


def settlement_rule(fixture, signal):
    """Select only the two verified football totals, preserving other rules."""
    identity = str(fixture.get('id') or '')
    if (re.fullmatch(r'leyu:[0-9]+', identity) is None
            or fixture.get('sport') != 'football'
            or signal.get('phase') != 'live'
            or signal.get('market_kind') != 'total'
            or signal.get('score_basis') != 'unknown'):
        return None
    periods = {'2': 'full_time', '18': 'first_half'}
    period = periods.get(str(signal.get('market_id')))
    if period is None or signal.get('period') != period:
        return None
    return {'reference': LEYU_PERIOD_TOTAL_RULE, 'score_basis': 'full_period',
            'provider': 'leyu', 'market_id': str(signal['market_id']), 'period': period}
