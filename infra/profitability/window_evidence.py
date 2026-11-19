"""Summarize local settlement, reconciliation and wallet evidence for fixed windows."""

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import re


def load_report(path):
    try:
        with path.open() as source:
            return json.load(source, parse_float=Decimal)
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def matches_window(report, start_key, end_key, begin, end):
    if not isinstance(report, dict):
        return False
    try:
        return datetime.fromisoformat(report[start_key]) == begin and datetime.fromisoformat(report[end_key]) == end
    except (KeyError, TypeError, ValueError):
        return False


def reconciliation(reports, window, begin, end):
    ordinal = window['ordinal']
    late_reports = [path for path in reports.glob(f'{ordinal}.late-*.json')
                    if path.stem.rsplit('-', 1)[-1].isdigit()]
    paths = sorted(late_reports, key=lambda path: int(path.stem.rsplit('-', 1)[-1]), reverse=True)
    paths += [reports / f'{ordinal}.retry.json', reports / f'{ordinal}.json']
    snapshot_digest = window.get('local_receipt_digest')
    if valid_digest(snapshot_digest):
        stem = f'{ordinal}.receipt-{snapshot_digest[3:]}'
        paths = [reports / f'{stem}.retry.json', reports / f'{stem}.json', *paths]
    fallback = None
    fallback_rank = (-1, -1)
    for path in paths:
        scan = load_report(path)
        if not isinstance(scan, list) or len(scan) != 1 or not matches_window(scan[0], 'begin', 'end', begin, end):
            continue
        report = scan[0]
        settled = window['settled']
        archived_digest = report.get('local_receipt_digest')
        ledger_matches = snapshot_digest == archived_digest if valid_digest(snapshot_digest) and valid_digest(archived_digest) else None
        count_matches = settled > 0 and report.get('complete_page') is True and (
            report.get('local_settled') == report.get('upstream_settled') == report.get('matched') == settled
            and report.get('upstream_only') == report.get('local_only') == 0
        )
        result = {
            'file': path.name, 'issues': report.get('issues'),
            'order_list_matches_current_count': count_matches,
            'local_receipt_digest_matches': ledger_matches,
            'settlement_window_attribution_complete': report.get('settlement_window_attribution_complete') is True,
            'order_receipt_fields_reconciled': report.get('order_receipt_fields_reconciled') is True,
            'current_receipt_fields_reconciled': count_matches and ledger_matches is True
            and report.get('order_receipt_fields_reconciled') is True,
        }
        if result['current_receipt_fields_reconciled']:
            return result
        rank = (ledger_matches is True, count_matches)
        if rank > fallback_rank:
            fallback = result
            fallback_rank = rank
    return fallback


def valid_digest(value):
    return isinstance(value, str) and re.fullmatch(r'v1:[0-9a-f]{64}', value) is not None


def streak_evidence(windows, ordinals, local_qualified):
    missing = [ordinal for ordinal in ordinals if ordinal not in windows]
    unverified = []
    for ordinal in ordinals:
        window = windows.get(ordinal)
        if window is None:
            continue
        scan = window['reconciliation'] or {}
        if (window['window_status'] != 'positive_receipts'
                or scan.get('current_receipt_fields_reconciled') is not True
                or scan.get('settlement_window_attribution_complete') is not True):
            unverified.append(ordinal)
    return {'ordinals': list(ordinals), 'missing_closed_windows': missing,
            'unverified_windows': unverified, 'local_streak_verified': local_qualified,
            'verified': local_qualified and len(ordinals) == 4 and not missing and not unverified}


def summarize(root, snapshot):
    anchor = datetime.fromisoformat(snapshot['anchor'])
    observed = datetime.fromisoformat(snapshot['observed_at'])
    if anchor.utcoffset() is None or observed.utcoffset() is None:
        raise ValueError('snapshot timestamps must include timezones')
    reports = root / 'reconciliations'
    items = []
    ordinals = set()
    for window in snapshot['windows']:
        ordinal = window['ordinal']
        if type(ordinal) is not int or ordinal < 0 or ordinal in ordinals:
            raise ValueError('invalid settlement window ordinal')
        ordinals.add(ordinal)
        begin = anchor + timedelta(hours=ordinal * 12)
        end = begin + timedelta(hours=12)
        closed = window['closed']
        if (type(closed) is not bool or closed != (end <= observed)
                or not matches_window(window, 'since', 'until', begin, end)):
            raise ValueError('window does not match the fixed settlement schedule')
        if not closed:
            continue

        scan = reconciliation(reports, window, begin, end)
        wallet = load_report(reports / f'{ordinal}.wallet.json')
        wallet_matches = matches_window(wallet, 'window_start', 'window_end', begin, end)
        wallet_totals = wallet.get('comparison') if wallet_matches else None
        items.append({
            'ordinal': ordinal, 'begin': begin, 'end': end, 'provider_net': window['provider_net'],
            'settled': window['settled'], 'window_status': window['status'],
            'missing_fee_field': window['missing_fee_field'], 'missing_currency': window['missing_currency'],
            'reconciliation': scan,
            'wallet': {
                **{key: wallet.get(key) for key in ('samples', 'unobserved_start_seconds', 'unobserved_end_seconds')},
                **{key: wallet_totals.get(key) for key in ('sampling_gaps', 'incomplete_intervals', 'intervals_with_difference')},
            } if isinstance(wallet_totals, dict) else None,
        })
    closed = {item['ordinal']: item for item in items}
    elapsed_windows = (observed - anchor) // timedelta(hours=12)
    rolling = range(max(0, elapsed_windows - 4), max(0, elapsed_windows))
    return {
        'anchor': anchor, 'observed_at': observed, 'windows': items,
        'initial_four_windows': streak_evidence(closed, range(4),
                                                snapshot.get('deadline_state') == 'positive_receipts_all_four'),
        'rolling_four_windows': streak_evidence(closed, rolling,
                                                snapshot.get('rolling_four_positive_receipts') is True),
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('monitor_dir', type=Path)
    parser.add_argument('--max-snapshot-age-seconds', type=int)
    arguments = parser.parse_args()
    if arguments.max_snapshot_age_seconds is not None and arguments.max_snapshot_age_seconds <= 0:
        parser.error('maximum snapshot age must be positive')
    snapshot = load_report(arguments.monitor_dir / 'state/latest.json')
    if not isinstance(snapshot, dict):
        parser.error('a readable current monitor snapshot is required')
    report = summarize(arguments.monitor_dir, snapshot)
    if arguments.max_snapshot_age_seconds is not None:
        age = (datetime.now(timezone.utc) - report['observed_at']).total_seconds()
        if not 0 <= age <= arguments.max_snapshot_age_seconds:
            parser.error('monitor snapshot is stale or dated in the future')
    print(json.dumps(report, default=str, ensure_ascii=False))
