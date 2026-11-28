"""Operator-only preview/apply/revoke CLI. No public endpoint and no order action."""
import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import uuid

import asyncpg

from .config import database_url
from .database import check_schema
from .expert_statistics_reset import apply_reset, preview_reset, revoke_reset


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    preview = actions.add_parser('preview')
    preview.add_argument('--output', required=True, type=Path)
    apply = actions.add_parser('apply')
    apply.add_argument('--plan', required=True, type=Path)
    apply.add_argument('--key', required=True)
    apply.add_argument('--reason', required=True)
    revoke = actions.add_parser('revoke')
    revoke.add_argument('--reset-id', required=True, type=uuid.UUID)
    revoke.add_argument('--reason', required=True)
    return parser.parse_args()


async def run(args):
    async def codecs(conn):
        await conn.set_type_codec('jsonb', schema='pg_catalog', encoder=json.dumps, decoder=json.loads)
    pool = await asyncpg.create_pool(database_url(), min_size=1, max_size=1, init=codecs,
        timeout=10, command_timeout=65,
        server_settings={'application_name':'expert-statistics-cycle', 'statement_timeout':'60000', 'lock_timeout':'3000'})
    try:
        async with pool.acquire() as conn:
            await check_schema(conn)
        if args.action == 'preview':
            async with pool.acquire() as conn:
                async with conn.transaction(isolation='repeatable_read', readonly=True):
                    plan = await preview_reset(conn)
            # Never overwrite the pre-change manifest; it is the reviewed restore evidence.
            descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w') as destination:
                json.dump(plan, destination, ensure_ascii=False, indent=2)
                destination.write('\n')
            return {'action':'preview', 'expert_count':len(plan['experts']), 'signal_count':len(plan['signals']),
                    'as_of':plan['as_of'], 'fingerprint':plan['fingerprint']}
        if args.action == 'apply':
            plan = json.loads(args.plan.read_text())
            return await apply_reset(pool, idempotency_key=args.key, expected_fingerprint=plan['fingerprint'],
                as_of=datetime.fromisoformat(plan['as_of']), reason=args.reason)
        return await revoke_reset(pool, reset_id=args.reset_id, reason=args.reason)
    finally:
        await pool.close()


if __name__ == '__main__':
    try:
        print(json.dumps(asyncio.run(run(arguments())), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'status':'failed', 'category':type(exc).__name__}))
        raise SystemExit(1) from None
