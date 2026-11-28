"""Run the existing profitability diagnostics against one read-only database snapshot."""

import asyncio
from datetime import timedelta
from hashlib import sha256
import json
import re
import sys
import tarfile


QUERIES = (
    'execution_quality', 'opportunity_coverage', 'review_latency', 'review_funnel',
    'market_refresh_coverage', 'review_price_coverage', 'snapshot_fill_comparison', 'order_overlap',
    'source_holdout', 'fixture_exposure', 'message_exposure', 'freshness_replay', 'cross_source_replay',
)
PSQL_DEFAULT = re.compile(r"\\if :\{\?(\w+)\}\n\\else\n\\set \1 ''\n\\endif\n")
PSQL_VARIABLE = re.compile(r"(?<![\w:]):'([a-z_]+)'")


def prepare_query(document, parameters):
    body = PSQL_DEFAULT.sub('', document)
    prelude, body = body.split('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n', 1)
    if any(line.strip() and not line.startswith('--') for line in prelude.splitlines()):
        raise ValueError('Unsupported SQL prelude')
    if not body.endswith('ROLLBACK;\n') or any(line.startswith('\\') for line in body.splitlines()):
        raise ValueError('Unsupported SQL body')
    # psql substitutes quoted text. Preserve that type in asyncpg before the SQL casts it.
    names = dict.fromkeys(PSQL_VARIABLE.findall(body))
    positions = {name: f'${number}::text' for number, name in enumerate(names, 1)}
    query = PSQL_VARIABLE.sub(lambda match: positions[match[1]], body.removesuffix('ROLLBACK;\n'))
    return query, [parameters[name].isoformat() for name in names]


async def collect(documents):
    import asyncpg
    from app.config import database_url

    conn = await asyncpg.connect(database_url(), timeout=5, command_timeout=9,
                                 server_settings={'TimeZone': 'UTC', 'default_transaction_read_only': 'on',
                                                  'statement_timeout': '7000', 'lock_timeout': '1000',
                                                  'application_name': 'profitability_analysis'})
    try:
        async with conn.transaction(isolation='repeatable_read', readonly=True):
            observed = await conn.fetchval('SELECT transaction_timestamp()')
            scope = await conn.fetchrow("""
                SELECT count(DISTINCT (account_id, provider_identity_hash)) AS bindings,
                       count(*) FILTER (WHERE account_id IS NULL OR provider_identity_hash IS NULL) AS missing_identity,
                       count(DISTINCT provider_currency) AS known_currencies
                FROM v2_bet_orders WHERE provider_order_no IS NOT NULL
            """)
            begin = observed - timedelta(hours=48)
            recent = {'begin': begin, 'end': observed, 'observed': observed, 'anchor': begin,
                      'analysis_end': observed, 'receipt_as_of': observed}
            # Allow native acceptance to reach the local ledger before including a placement in a replay.
            replay_end = observed - timedelta(minutes=5)
            placement = recent | {'begin': replay_end - timedelta(hours=48),
                                  'split': replay_end - timedelta(hours=24), 'end': replay_end}
            replay = placement | {'begin': replay_end - timedelta(days=35), 'split': replay_end - timedelta(days=7)}
            scopes = {'source_holdout': replay, 'fixture_exposure': placement, 'message_exposure': placement,
                      'freshness_replay': placement, 'cross_source_replay': placement}
            results = {}
            for name in QUERIES:
                raw = documents[name]
                result = {'sql_sha256': sha256(raw).hexdigest()}
                if name == 'execution_quality' and (scope['bindings'] != 1 or scope['missing_identity']
                                                     or scope['known_currencies'] > 1):
                    results[name] = result | {'error': 'incomparable_ledger_scope'}
                    continue
                try:
                    query, args = prepare_query(raw.decode('utf-8'), scopes.get(name, recent))
                    async with conn.transaction():
                        value = await conn.fetchval(query, *args)
                        if not isinstance(json.loads(value), dict):
                            raise ValueError('Diagnostic did not return a JSON object')
                    # Keep JSON number precision when merging the aggregate results.
                    result['json'] = value
                except (asyncpg.PostgresError, asyncio.TimeoutError, KeyError, TypeError, ValueError) as error:
                    result['error'] = type(error).__name__
                results[name] = result
            return {'observed_at': observed.isoformat(), 'ledger_scope': dict(scope), 'diagnostics': results}
    finally:
        await conn.close()


def main():
    try:
        with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as archive:
            documents = {entry.name.removesuffix('.sql'): archive.extractfile(entry).read()
                         for entry in archive if entry.isreg() and entry.name in {f'{name}.sql' for name in QUERIES}}
        if set(documents) != set(QUERIES):
            raise ValueError('Diagnostic SQL set incomplete')
        print(json.dumps(asyncio.run(collect(documents))))
    except Exception as error:
        raise SystemExit(type(error).__name__) from None


if __name__ == '__main__':
    main()
