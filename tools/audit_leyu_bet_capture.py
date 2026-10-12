#!/usr/bin/env python3
"""Replay captured preflight responses offline, without exposing credentials."""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector.leyu_account import (  # noqa: E402
    LeyuAccountClient, VENUE_AMOUNT_PATH, VENUE_BET_PATH,
    VENUE_LATEST_MARKET_PATH, VENUE_LIMIT_PATH,
)
from collector.leyu_client import SUCCESS_CODES, decode_envelope  # noqa: E402
from collector.session import SessionError  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=Path)
    args = parser.parse_args()
    captured = {}
    with zipfile.ZipFile(args.capture) as archive:
        for name in archive.namelist():
            if not name.endswith('/request.json'):
                continue
            metadata = json.loads(archive.read(name))
            path = urlsplit(metadata['url']).path
            if path not in (VENUE_AMOUNT_PATH, VENUE_BET_PATH,
                            VENUE_LATEST_MARKET_PATH, VENUE_LIMIT_PATH):
                continue
            prefix = name.rsplit('/', 1)[0]
            body_name = prefix + '/request_body.json'
            request = json.loads(archive.read(body_name)) if body_name in archive.namelist() else None
            response = json.loads(archive.read(prefix + '/response_body.json'))
            captured.setdefault(path, []).append((metadata['time'], request, response))
    _, bet, _ = sorted(captured[VENUE_BET_PATH])[0]
    detail = dict(bet['seriesOrders'][0]['orderDetailList'][0])
    responses = {path: decode_envelope(sorted(captured[path])[0][2])
                 for path in (VENUE_LATEST_MARKET_PATH, VENUE_LIMIT_PATH, VENUE_AMOUNT_PATH)}
    # The App displays +0.5 in the bet DTO; the model uses the market's 0.5.
    # Normalize only for this audit so the independent limit mismatch is visible.
    market = next(row for row in responses[VENUE_LATEST_MARKET_PATH]
                  if str(row['id']) == str(detail['marketId']))
    detail['marketValue'] = market['marketValue']
    detail.setdefault('scoreBenchmark', '')
    client = LeyuAccountClient(SimpleNamespace(app_host='https://offline.invalid'))
    calls = []

    def offline_request(path, body=None, **kwargs):
        calls.append(path)
        return responses[path]

    client._venue_request = offline_request
    try:
        client.prepare_bet(detail, float(detail['betAmount']))
        outcome = 'passed'
    except SessionError as exc:
        outcome = str(exc)
    limit = next(row for row in responses[VENUE_LIMIT_PATH]
                 if str(row['playOptionsId']) == str(detail['playOptionsId']))
    print(json.dumps({
        'outcome': outcome,
        'option_id_matches': True,
        'legacy_play_id_matches': str(limit.get('playId')) == str(detail['playId']),
        'legacy_type_passes': str(limit.get('type', '1')) == '1',
        'business_code_succeeds': str(limit.get('code', '0')) in SUCCESS_CODES,
        'stake_within_bounds': float(limit['minBet']) <= float(detail['betAmount']) <= float(limit['orderMaxPay']),
        'called_paths': calls, 'network_requests': 0, 'submit_calls': 0,
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
