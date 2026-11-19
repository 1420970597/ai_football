"""Capture public ESPN score snapshots and events for a manually matched fixture."""

import argparse
from datetime import datetime, timezone
from http.client import HTTPException
import json
import re
from time import monotonic, sleep
from urllib.request import urlopen


SUMMARY = 'https://site.api.espn.com/apis/site/v2/sports'
STATE_KEYS = ('clock', 'displayClock', 'period', 'type')
PLAY_KEYS = ('id', 'sequenceNumber', 'wallclock', 'period', 'clock', 'homeScore', 'awayScore', 'type', 'scoringPlay')
SOCCER_EVENT_KEYS = ('id', 'wallclock', 'period', 'clock', 'type', 'scoringPlay', 'shootout')


def soccer_red_cards(document, competitors):
    identities = {row['team'].get('id'): row['homeAway'] for row in competitors}
    counts = {}
    if None in identities or len(identities) != 2:
        return counts
    for row in document.get('boxscore', {}).get('teams', []):
        side = identities.get(row['team']['id'])
        if side is None:
            continue
        values = [stat.get('displayValue') for stat in row.get('statistics', [])
                  if stat.get('name') == 'redCards']
        if side in counts or len(values) != 1 or not re.fullmatch(r'[0-9]{1,2}', str(values[0])):
            counts[side] = None
            continue
        counts[side] = int(values[0])
    return {side: counts.get(side) for side in ('home', 'away')}


def game_state(document, event_id, home, away, *, league=None, kickoff=None):
    header = document['header']
    game, = header['competitions']
    teams = {row['homeAway']: (row['team']['abbreviation'], row.get('score')) for row in game['competitors']}
    if (header['id'] != event_id or game['id'] != event_id or len(game['competitors']) != 2
            or {side: info[0] for side, info in teams.items()} != {'home': home, 'away': away}):
        raise ValueError('event identity mismatch')
    state = {
        'event': event_id, 'scheduled_start': game['date'], 'teams': teams,
        'status': {key: game['status'][key] for key in STATE_KEYS if key in game['status']},
    }
    if league is None:
        return state | {'plays': [{key: play.get(key) for key in PLAY_KEYS} for play in document.get('plays', [])]}
    if header['league']['slug'] != league or datetime.fromisoformat(game['date']) != kickoff:
        raise ValueError('soccer event league or kickoff mismatch')
    events = document.get('keyEvents')
    team_ids = {side['homeAway']: side['team'].get('id') for side in game['competitors']}
    return state | {
        'league': league, 'wallclock_available': document.get('wallclockAvailable'),
        'team_ids': team_ids, 'red_cards': soccer_red_cards(document, game['competitors']),
        'key_events_available': isinstance(events, list),
        'key_events': [{key: event.get(key) for key in SOCCER_EVENT_KEYS}
                       | {'team_id': (event.get('team') or {}).get('id')}
                       for event in (events if isinstance(events, list) else [])],
    }


def capture(event_id, home, away, *, league=None, kickoff=None):
    requested_at = datetime.now(timezone.utc)
    feed = f'soccer/{league}' if league else 'basketball/nba'
    try:
        with urlopen(f'{SUMMARY}/{feed}/summary?event={event_id}', timeout=8) as response:
            raw = response.read(3_000_001)
            received_at = datetime.now(timezone.utc)
        if len(raw) > 3_000_000:
            raise ValueError('event summary size limit')
        result = game_state(json.loads(raw), event_id, home, away, league=league, kickoff=kickoff)
    except (HTTPException, KeyError, TypeError, ValueError, OSError) as error:
        received_at = datetime.now(timezone.utc)
        result = {'event': event_id, 'error': type(error).__name__, 'http_status': getattr(error, 'code', None)}
    return result | {'requested_at': requested_at.isoformat(), 'received_at': received_at.isoformat()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('event_id')
    parser.add_argument('home')
    parser.add_argument('away')
    parser.add_argument('--soccer-league')
    parser.add_argument('--kickoff')
    parser.add_argument('--samples', type=int, default=1)
    parser.add_argument('--interval-seconds', type=int, default=30)
    args = parser.parse_args()
    if (not re.fullmatch(r'[0-9]{1,16}', args.event_id)
            or any(not re.fullmatch(r'[A-Z]{2,4}', team) for team in (args.home, args.away))
            or args.home == args.away):
        parser.error('provide one native event ID and two distinct team abbreviations')
    kickoff = None
    if args.soccer_league or args.kickoff:
        try:
            kickoff = datetime.fromisoformat(args.kickoff or '')
            if (kickoff.utcoffset() is None or not args.soccer_league
                    or not re.fullmatch(r'[a-z0-9]+(?:\.[a-z0-9]+)*', args.soccer_league)):
                raise ValueError
        except ValueError:
            parser.error('soccer observations require a league and an explicit kickoff timezone')
    if not 1 <= args.samples <= 238 or not 20 <= args.interval_seconds <= 300:
        parser.error('samples must be 1-238 and interval must be 20-300 seconds')
    for index in range(args.samples):
        started_at = monotonic()
        print(json.dumps(capture(args.event_id, args.home, args.away,
                                 league=args.soccer_league, kickoff=kickoff)), flush=True)
        if index + 1 < args.samples:
            sleep(max(0, args.interval_seconds - (monotonic() - started_at)))


if __name__ == '__main__':
    main()
