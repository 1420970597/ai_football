"""Capture public NBA scores and play versions for a manually matched fixture."""

import argparse
from datetime import datetime, timezone
from http.client import HTTPException
import json
import re
from time import monotonic, sleep
from urllib.request import urlopen


SUMMARY = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event='
STATE_KEYS = ('clock', 'displayClock', 'period', 'type')
PLAY_KEYS = ('id', 'sequenceNumber', 'wallclock', 'period', 'clock', 'homeScore', 'awayScore', 'type', 'scoringPlay')


def game_state(document, event_id, home, away):
    header = document['header']
    game, = header['competitions']
    teams = {row['homeAway']: (row['team']['abbreviation'], row.get('score')) for row in game['competitors']}
    if (header['id'] != event_id or game['id'] != event_id or len(game['competitors']) != 2
            or {side: info[0] for side, info in teams.items()} != {'home': home, 'away': away}):
        raise ValueError('basketball event identity mismatch')
    return {
        'event': event_id, 'scheduled_start': game['date'], 'teams': teams,
        'status': {key: game['status'][key] for key in STATE_KEYS if key in game['status']},
        'plays': [{key: play.get(key) for key in PLAY_KEYS} for play in document.get('plays', [])],
    }


def capture(event_id, home, away):
    requested_at = datetime.now(timezone.utc)
    try:
        with urlopen(SUMMARY + event_id, timeout=8) as response:
            raw = response.read(3_000_001)
            received_at = datetime.now(timezone.utc)
        if len(raw) > 3_000_000:
            raise ValueError('basketball summary size limit')
        result = game_state(json.loads(raw), event_id, home, away)
    except (HTTPException, KeyError, TypeError, ValueError, OSError) as error:
        received_at = datetime.now(timezone.utc)
        result = {'event': event_id, 'error': type(error).__name__, 'http_status': getattr(error, 'code', None)}
    return result | {'requested_at': requested_at.isoformat(), 'received_at': received_at.isoformat()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('event_id')
    parser.add_argument('home')
    parser.add_argument('away')
    parser.add_argument('--samples', type=int, default=1)
    parser.add_argument('--interval-seconds', type=int, default=30)
    args = parser.parse_args()
    if (not re.fullmatch(r'[0-9]{1,16}', args.event_id)
            or any(not re.fullmatch(r'[A-Z]{2,4}', team) for team in (args.home, args.away))
            or args.home == args.away):
        parser.error('provide one native event ID and two distinct NBA abbreviations')
    if not 1 <= args.samples <= 20 or not 20 <= args.interval_seconds <= 300:
        parser.error('samples must be 1-20 and interval must be 20-300 seconds')
    for index in range(args.samples):
        started_at = monotonic()
        print(json.dumps(capture(args.event_id, args.home, args.away)), flush=True)
        if index + 1 < args.samples:
            sleep(max(0, args.interval_seconds - (monotonic() - started_at)))


if __name__ == '__main__':
    main()
