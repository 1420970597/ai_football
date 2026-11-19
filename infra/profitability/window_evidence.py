"""Summarize local settlement, reconciliation and wallet evidence for fixed windows."""

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re


def load_report(path, captured=None, root=None):
    try:
        raw = path.read_bytes()
        body = raw.decode('utf-8')
        report = json.loads(body, parse_float=Decimal)
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if captured is not None:
        captured[path.relative_to(root).as_posix()] = {'sha256': sha256(raw).hexdigest(), 'json': body}
    return report


def matches_window(report, start_key, end_key, begin, end):
    if not isinstance(report, dict):
        return False
    try:
        return datetime.fromisoformat(report[start_key]) == begin and datetime.fromisoformat(report[end_key]) == end
    except (KeyError, TypeError, ValueError):
        return False


def reconciliation(reports, window, begin, end, *, read=load_report, late_paths=None):
    ordinal = window['ordinal']
    paths_for_ordinal = reports.glob(f'{ordinal}.late-*.json') if late_paths is None else late_paths(reports, ordinal)
    late_reports = [path for path in paths_for_ordinal
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
        scan = read(path)
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


def positive_net(value):
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return False
    return amount.is_finite() and amount > 0


def streak_evidence(windows, ordinals, local_qualified):
    missing = [ordinal for ordinal in ordinals if ordinal not in windows]
    unverified = []
    for ordinal in ordinals:
        window = windows.get(ordinal)
        if window is None:
            continue
        scan = window['reconciliation'] or {}
        if (window['window_status'] != 'positive_receipts'
                or not positive_net(window['provider_net'])
                or window['missing_fee_field'] != 0 or window['missing_currency'] != 0
                or scan.get('current_receipt_fields_reconciled') is not True
                or scan.get('settlement_window_attribution_complete') is not True):
            unverified.append(ordinal)
    return {'ordinals': list(ordinals), 'missing_closed_windows': missing,
            'unverified_windows': unverified, 'local_streak_verified': local_qualified,
            'verified': local_qualified and len(ordinals) == 4 and not missing and not unverified}


def summarize(root, snapshot, *, read=load_report, late_paths=None):
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

        scan = reconciliation(reports, window, begin, end, read=read, late_paths=late_paths)
        wallet = read(reports / f'{ordinal}.wallet.json')
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


def verify_archive(document):
    if not isinstance(document, dict) or document.get('evidence_format') != 1:
        raise ValueError('archive has no replayable evidence')
    inputs = document['input_files']
    if not isinstance(inputs, dict):
        raise ValueError('invalid evidence inputs')
    reports = {}
    for name, record in inputs.items():
        if name != 'state/latest.json' and not re.fullmatch(r'reconciliations/[0-9][a-z0-9.-]*\.json', name):
            raise ValueError('unexpected evidence name')
        body = record['json']
        if sha256(body.encode('utf-8')).hexdigest() != record['sha256']:
            raise ValueError('evidence input hash mismatch')
        reports[Path(name)] = json.loads(body, parse_float=Decimal)

    def late_paths(path, ordinal):
        return [name for name in reports if name.parent == path and name.match(f'{ordinal}.late-*.json')]

    replay = summarize(Path('.'), reports[Path('state/latest.json')], read=reports.get, late_paths=late_paths)
    recorded = {key: value for key, value in document.items() if key not in {'input_files', 'evidence_format'}}
    if json.loads(json.dumps(replay, default=str), parse_float=Decimal) != recorded:
        raise ValueError('recorded summary differs from replay')
    return replay


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('monitor_dir', type=Path, nargs='?')
    parser.add_argument('--max-snapshot-age-seconds', type=int)
    parser.add_argument('--include-inputs', action='store_true')
    parser.add_argument('--verify-archive', type=Path)
    arguments = parser.parse_args()
    if arguments.verify_archive:
        if arguments.monitor_dir or arguments.include_inputs or arguments.max_snapshot_age_seconds is not None:
            parser.error('archive verification does not use live inputs')
        try:
            report = verify_archive(load_report(arguments.verify_archive))
        except (KeyError, AttributeError, TypeError, ValueError) as error:
            parser.error(f'invalid evidence archive ({error})')
        print(json.dumps(report, default=str, ensure_ascii=False))
        raise SystemExit(0)
    if arguments.monitor_dir is None:
        parser.error('provide a monitor directory or an evidence archive')
    if arguments.max_snapshot_age_seconds is not None and arguments.max_snapshot_age_seconds <= 0:
        parser.error('maximum snapshot age must be positive')
    captured = {} if arguments.include_inputs else None

    def read_input(path):
        if captured is not None and (previous := captured.get(path.relative_to(arguments.monitor_dir).as_posix())):
            return json.loads(previous['json'], parse_float=Decimal)
        return load_report(path, captured, arguments.monitor_dir)

    snapshot = read_input(arguments.monitor_dir / 'state/latest.json')
    if not isinstance(snapshot, dict):
        parser.error('a readable current monitor snapshot is required')
    report = summarize(arguments.monitor_dir, snapshot, read=read_input)
    if arguments.max_snapshot_age_seconds is not None:
        age = (datetime.now(timezone.utc) - report['observed_at']).total_seconds()
        if not 0 <= age <= arguments.max_snapshot_age_seconds:
            parser.error('monitor snapshot is stale or dated in the future')
    if captured is not None:
        report = report | {'evidence_format': 1, 'input_files': captured}
    print(json.dumps(report, default=str, ensure_ascii=False))
