"""Count and fingerprint COPY streams without exposing stored row contents."""
from __future__ import annotations

import hashlib
import json
import re
import sys

COPY = re.compile(rb'^COPY public\.(v2_[a-z0-9_]+) \(([^\r\n]+)\) FROM stdin;\r?\n$')
MAX_LINE = 64 * 1024 * 1024


def copy_manifest(stream):
    tables = {}
    name = None
    digest = None
    row_sum = row_xor = 0
    order = []
    while True:
        line = stream.readline(MAX_LINE + 1)
        if not line:
            break
        if len(line) > MAX_LINE:
            raise ValueError('copy_line_budget_exceeded')
        if name is not None:
            if line in (b'\\.\n', b'\\.\r\n'):
                tables[name]['ordered_copy_sha256'] = digest.hexdigest()
                tables[name]['row_sha256_sum_mod_2_256'] = format(row_sum, '064x')
                tables[name]['row_sha256_xor'] = format(row_xor, '064x')
                name = None
            else:
                tables[name]['rows'] += 1
                digest.update(line)
                fields = line.rstrip(b'\r\n').split(b'\t')
                if len(fields) != len(order):
                    raise ValueError('copy_column_count_mismatch')
                canonical = b'\t'.join(fields[i] for i in order)
                hashed = int.from_bytes(hashlib.sha256(canonical).digest(), 'big')
                row_sum = (row_sum + hashed) % (1 << 256)
                row_xor ^= hashed
        elif line.startswith(b'COPY '):
            match = COPY.fullmatch(line)
            if match is None:
                raise ValueError('unexpected_copy_contract')
            name = match[1].decode('ascii')
            if name in tables:
                raise ValueError('duplicate_copy_table')
            tables[name] = {'rows': 0}
            digest = hashlib.sha256()
            columns = [value.strip().strip(b'"') for value in match[2].split(b',')]
            if not all(re.fullmatch(rb'[a-z_][a-z0-9_]*', value) for value in columns):
                raise ValueError('unexpected_copy_column')
            if len(columns) != len(set(columns)):
                raise ValueError('duplicate_copy_column')
            order = sorted(range(len(columns)), key=columns.__getitem__)
            tables[name]['columns'] = [columns[i].decode('ascii') for i in order]
            row_sum = row_xor = 0
    if name is not None:
        raise ValueError('incomplete_copy_stream')
    if not tables:
        raise ValueError('copy_stream_missing')
    return {'format': 'platform-v2-copy-manifest-2', 'contains_row_contents': False,
            'ordered_hash_requires_same_export_order': True,
            'tables': tables, 'total_rows': sum(t['rows'] for t in tables.values())}


if __name__ == '__main__':
    try:
        print(json.dumps(copy_manifest(sys.stdin.buffer), sort_keys=True))
    except (ValueError, OSError) as exc:
        print(json.dumps({'state': 'failed', 'category': str(exc) if isinstance(exc, ValueError) else type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1)
