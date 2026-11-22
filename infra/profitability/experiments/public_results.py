"""Read-only adapter for public Football-Data.co.uk result and closing-odds CSV files.

Public format (https://www.football-data.co.uk/downloadm.php, offline copy only):
one row per match with ``Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,...`` followed
by decimal bookmaker odds. Pre-match columns are prefixed ``B365``/``PS``/``Avg`` and
the closing snapshot is prefixed ``B365C``/``PSC``/``AvgC`` with a ``H/D/A`` suffix.
``Date`` in these files is ``DD/MM/YYYY`` (older files use a two-digit year) and
``Time`` is the source's local kickoff clock. The archive does not state the timezone,
so kickoff instants stay unverified and are never used as a settlement time.

Nothing here performs network access; the CSV directory is mounted read-only.
"""

import csv
import io
from collections import Counter
from datetime import date
from pathlib import Path

from .evidence import decimal, file_record, read_bytes

REQUIRED = ('Div', 'Date', 'Time', 'HomeTeam', 'AwayTeam', 'FTHG', 'FTAG', 'FTR')
CLOSING_BOOKS = {
    'B365C': 'bet365 closing snapshot',
    'PSC': 'Pinnacle closing snapshot',
    'AvgC': 'market-average closing snapshot',
}
OPENING_BOOKS = {
    'B365': 'bet365 pre-match snapshot',
    'PS': 'Pinnacle pre-match snapshot',
    'Avg': 'market-average pre-match snapshot',
}
RESULTS = {'H': 'home', 'D': 'draw', 'A': 'away'}


def parse_date(text):
    parts = str(text).strip().split('/')
    if len(parts) != 3:
        raise ValueError(f'unrecognised date {text!r}')
    try:
        day, month, year = (int(part) for part in parts)
    except ValueError as error:
        raise ValueError(f'unrecognised date {text!r}') from error
    if year < 100:
        year += 2000
    return date(year, month, day)


def parse_time(text):
    text = str(text or '').strip()
    if not text:
        return None
    try:
        hour, minute = text.split(':')[:2]
        return f'{int(hour):02d}:{int(minute):02d}'
    except ValueError as error:
        raise ValueError(f'unrecognised time {text!r}') from error


def _odds(row, prefix):
    """Return the three-way closing/opening odds for one book, or None if partial."""
    fields = [f'{prefix}{suffix}' for suffix in ('H', 'D', 'A')]
    cells = [row.get(field) for field in fields]
    present = [bool(str(cell or '').strip()) for cell in cells]
    if not any(present):
        return None, 'absent'
    if not all(present):
        return None, 'partial'
    try:
        prices = {'home': decimal(cells[0], name=fields[0]),
                  'draw': decimal(cells[1], name=fields[1]),
                  'away': decimal(cells[2], name=fields[2])}
    except ValueError:
        return None, 'invalid'
    if any(price <= 1 for price in prices.values()):
        return None, 'invalid'
    return prices, 'complete'


def read_matches(path, root=None, season=None):
    """Parse one public CSV file without mutating it."""
    path = Path(path)
    record = file_record(path, root)
    text = read_bytes(path).decode('utf-8-sig')
    matches, skipped = [], Counter()
    seen = set()
    for line, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):
        if not all(str(row.get(field) or '').strip() for field in REQUIRED):
            skipped['missing_required_field'] += 1
            continue
        try:
            played = parse_date(row['Date'])
            goals = (int(row['FTHG']), int(row['FTAG']))
            result = str(row['FTR']).strip().upper()
            if goals[0] < 0 or goals[1] < 0 or result not in RESULTS:
                raise ValueError('invalid result')
            observed = 'home' if goals[0] > goals[1] else 'away' if goals[1] > goals[0] else 'draw'
            if RESULTS[result] != observed:
                raise ValueError('result disagrees with goals')
        except ValueError:
            skipped['invalid_result_row'] += 1
            continue
        key = (row['Div'].strip(), played, row['HomeTeam'].strip(), row['AwayTeam'].strip())
        if key in seen:
            skipped['duplicate_fixture_row'] += 1
            continue
        seen.add(key)
        closing, opening = {}, {}
        for books, target in ((CLOSING_BOOKS, closing), (OPENING_BOOKS, opening)):
            for prefix in books:
                prices, state = _odds(row, prefix)
                if state == 'complete':
                    target[prefix] = prices
                elif state != 'absent':
                    skipped[f'{state}_odds_{prefix}'] += 1
        clock = parse_time(row['Time'])
        matches.append({
            'div': key[0],
            'season': season or path.stem.split('_')[-1],
            'source': record['path'],
            'source_sha256': record['sha256'],
            'row': line,
            'date': played.isoformat(),
            'time': clock,
            'kickoff_local': f'{played.isoformat()}T{clock}' if clock else None,
            'home': key[2],
            'away': key[3],
            'home_goals': goals[0],
            'away_goals': goals[1],
            'result': RESULTS[result],
            'closing': closing,
            'opening': opening,
        })
    return {'matches': matches, 'skipped': skipped, 'provenance': record}


def load_directory(path, root=None):
    """Read every ``*.csv`` in a directory; deterministic order by file name."""
    path = Path(path)
    if not path.is_dir():
        raise FileNotFoundError(f'public CSV directory is not mounted: {path}')
    matches, skipped, provenance = [], Counter(), []
    for file in sorted(path.glob('*.csv')):
        document = read_matches(file, root)
        matches.extend(document['matches'])
        skipped.update(document['skipped'])
        provenance.append(document['provenance'])
    return {'matches': matches, 'skipped': skipped, 'provenance': provenance}


def closing_coverage(matches):
    """Count the matches that carry a complete closing triple per book."""
    coverage = dict.fromkeys(CLOSING_BOOKS, 0)
    for match in matches:
        for book in CLOSING_BOOKS:
            if book in match['closing']:
                coverage[book] += 1
    return {'matches': len(matches), 'closing_odds_complete': coverage}
