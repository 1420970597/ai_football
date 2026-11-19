"""Bind additional half-line markets using a verified fixture identity."""

import argparse
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import re

from reference_history import GAMMA, get_json
from reference_orderbooks import now, polymarket_terms


KINDS = {'totals': ('total', 'full_time', 'total'),
         'first_half_totals': ('total', 'first_half', 'first-half-total'),
         'spreads': ('handicap', 'full_time', 'spread'),
         'soccer_team_totals': ('team_total', 'full_time', 'team-total'),
         'soccer_first_half_team_totals': ('team_total', 'first_half', 'first-half-team-total')}


def bind_catalog(catalog, label, fixture, home, away, kickoff, existing):
    if (catalog['slug'] != label + '-more-markets'
            or datetime.fromisoformat(catalog['startTime']) != kickoff):
        raise ValueError('catalog fixture or kickoff differs from the reviewed fixture')
    teams = sorted(catalog['teams'], key=lambda team: team['ordering'], reverse=True)
    if [(team['ordering'], team['abbreviation']) for team in teams] != [('home', home), ('away', away)]:
        raise ValueError('catalog teams differ from the reviewed home and away teams')
    names = {team['ordering']: team['name'] for team in teams}
    bindings, outcomes = [], {}
    for raw in catalog['markets']:
        slug, market_type = raw['slug'], raw.get('sportsMarketType')
        if slug in existing or market_type not in KINDS or raw.get('acceptingOrders') is not True:
            continue
        terms, sides = polymarket_terms(slug, raw, now())
        if datetime.fromisoformat(terms['scheduled_start']) != kickoff or slug in outcomes:
            raise ValueError('market kickoff differs or its identifier is duplicated')
        kind, period, prefix = KINDS[market_type]
        side_suffix = '-(?P<side>home|away)' if kind in {'handicap', 'team_total'} else ''
        identifier = re.fullmatch(re.escape(f'{label}-{prefix}') + side_suffix + r'-(?P<line>[0-9]+)pt5', slug)
        if not identifier:
            raise ValueError('market identifier does not describe its half-line contract')
        line = Decimal(identifier['line'] + '.5')
        side = identifier.groupdict().get('side')
        labels = dict(zip(('Over', 'Under'), ('over', 'under')))
        if kind == 'handicap':
            line = -line
            if tuple(label for label, _ in sides) != (names[side], names['away' if side == 'home' else 'home']):
                raise ValueError('handicap direction differs from the participant order')
            labels = {name: position for position, name in names.items()}
        if (Decimal(str(terms['line'])) != line or set(labels) != {label for label, _ in sides}
                or kind == 'team_total' and names[side] not in raw['question'].split(': ', 1)[-1]):
            raise ValueError('market line or participants differ from the catalog contract')
        binding = {'fixture_id': fixture, 'source': 'polymarket', 'market': slug, 'kind': kind,
                   'period': period, 'line': str(-line if kind == 'handicap' and side == 'away' else line),
                   'rules_sha256': terms['rules_sha256']}
        if kind == 'team_total':
            binding['team_side'] = side
        bindings.append(binding)
        outcomes[slug] = {'rules_sha256': terms['rules_sha256'], 'labels': labels}
    if not bindings:
        raise ValueError('no additional open contracts were verified')
    return {'event': catalog['slug'], 'observed_at': now().isoformat(), 'bindings': bindings,
            'outcomes': {'polymarket': outcomes}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ('label', 'fixture', 'home', 'away', 'kickoff'):
        parser.add_argument(field)
    parser.add_argument('existing', type=Path)
    args = parser.parse_args()
    kickoff = datetime.fromisoformat(args.kickoff)
    if kickoff.utcoffset() is None or not re.fullmatch(r'[a-z0-9-]{1,146}', args.label):
        parser.error('invalid reviewed event or kickoff')
    existing = {entry['market'] for entry in json.loads(args.existing.read_bytes())
                if entry['source'] == 'polymarket'}
    catalog = get_json(f'{GAMMA}/events/slug/{args.label}-more-markets')
    print(json.dumps(bind_catalog(catalog, args.label, args.fixture, args.home, args.away, kickoff, existing),
                     indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
