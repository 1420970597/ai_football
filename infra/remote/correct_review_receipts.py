"""Explicit, bounded maintenance CLI. Default is a read-only preview."""
import argparse
import asyncio
import json
from pathlib import Path
import uuid

import asyncpg
from app.config import load_settings
from app.review_receipt_correction import correct_recovered_receipt


async def run(args):
    raw = json.loads(args.jobs_file.read_text())
    if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
        raise ValueError('expected 1..100 explicit job identifiers')
    jobs = [uuid.UUID(item) for item in raw]
    if len(set(jobs)) != len(jobs):
        raise ValueError('duplicate job identifier')
    conn = await asyncpg.connect(load_settings(role='api').database_url, timeout=5, command_timeout=8,
        max_cached_statement_lifetime=0, server_settings={
            'application_name': 'receipt-correction-maintenance', 'statement_timeout': '7000',
            'lock_timeout': '1000', 'default_transaction_read_only': 'off' if args.apply else 'on'})
    try:
        await conn.set_type_codec('jsonb', schema='pg_catalog', encoder=json.dumps, decoder=json.loads)
        async with asyncio.timeout(900):
            for job in jobs:
                async with conn.transaction(readonly=not args.apply):
                    result = await correct_recovered_receipt(conn, job, apply=args.apply)
                print(json.dumps(result), flush=True)
    finally:
        await conn.close(timeout=5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs-file', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    asyncio.run(run(parser.parse_args()))
