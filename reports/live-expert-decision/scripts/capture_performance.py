#!/usr/bin/env python3
"""Record aggregate live performance; never save credentials or match payloads."""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_URL = 'http://127.0.0.1:3003/api/v1/workbench?type=real'
DEFAULT_SAMPLES = 120
DEFAULT_INTERVAL_S = 1.0
TIMEOUT_S = 12
QUANTILES = ('p50', 'p95', 'p99', 'max')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=DEFAULT_URL)
    parser.add_argument('--samples', type=int, default=DEFAULT_SAMPLES)
    parser.add_argument('--interval', type=float, default=DEFAULT_INTERVAL_S)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 1 or args.interval <= 0:
        parser.error('samples and interval must be positive')
    rows = []
    for i in range(args.samples):
        start = time.monotonic()
        headers = {'Accept-Encoding': 'gzip' if i % 2 else 'identity'}
        request = urllib.request.Request(args.url, headers=headers)
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            body = response.read()
            encoding = response.headers.get('Content-Encoding', 'identity')
            response_ms = (time.monotonic() - start) * 1000
        decoded = gzip.decompress(body) if encoding == 'gzip' else body
        data = json.loads(decoded)
        perf, rt = data['performance'], data['realtime']
        row = {'at': datetime.now(timezone.utc).isoformat(), 'response_ms': round(response_ms, 3),
               'encoding': encoding, 'wire_bytes': len(body), 'decoded_bytes': len(decoded),
               'matches': data['count'], 'computations': perf['computations'],
               'rolling_samples': perf['samples'], 'connected': rt['connected'],
               'messages': rt['messages'], 'price_ticks': rt['price_ticks'],
               'reconnects': rt['reconnects'], 'errors': perf['errors'],
               'superseded': perf['superseded'], 'journal_dropped': perf['journal_dropped']}
        for metric in ('compute_ms', 'event_to_result_ms'):
            for quantile in QUANTILES:
                row[metric + '_' + quantile] = perf[metric][quantile]
        for kind in ('real', 'virtual', 'unknown'):
            values = perf.get('by_type_event_to_result_ms', {}).get(kind, {})
            row[kind + '_samples'] = values.get('samples', 0)
            for quantile in QUANTILES:
                row[kind + '_latency_ms_' + quantile] = values.get(quantile)
        rows.append(row)
        if i + 1 < args.samples:
            time.sleep(max(0, args.interval - (time.monotonic() - start)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({'observations': len(rows), 'first': rows[0], 'last': rows[-1]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
