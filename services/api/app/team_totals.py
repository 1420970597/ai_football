"""Native football team identity; no inferred aliases or provider score rule."""
import re


def team_total_terms(quote, fixture_id):
    if (re.fullmatch(r'leyu:[0-9]+', str(fixture_id)) is None
            or quote.get('market_kind') != 'team_total'
            or quote.get('period') != 'full_time'):
        return {}
    side = {'10': 'home', '11': 'away'}.get(str(quote.get('market_id')))
    if side is None:
        return {}
    # Signal rows retain the immutable native market id; current quotes also
    # supply the independently checked play id.
    if quote.get('play_id') is not None and str(quote['play_id']) != str(quote['market_id']):
        return {}
    return {'team_side': side}
