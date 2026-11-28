"""Stable quote identities: update changed rows; insert missing rows with conflict revalidation."""
from collections import Counter


MIN_PREFETCH_QUOTES_PER_FIXTURE = 256

def quote_update_condition(current: str, incoming: str) -> str:
    """SQL aliases are fixed by this module; values remain bound parameters."""
    return (
        f"coalesce({incoming}.source_updated_at,{incoming}.received_at)>=coalesce({current}.source_updated_at,{current}.received_at) "
        f"AND (ROW({current}.market_name,{current}.market_kind,{current}.line,"
        f"{current}.price,{current}.source_updated_at,{current}.source_time_quality,"
        f"{current}.status,{current}.play_id,{current}.market_instance_id,"
        f"{current}.selection_id,{current}.is_main,{current}.score_basis) "
        f"IS DISTINCT FROM ROW({incoming}.market_name,{incoming}.market_kind,{incoming}.line,{incoming}.price,"
        f"{incoming}.source_updated_at,{incoming}.source_time_quality,{incoming}.status,{incoming}.play_id,"
        f"{incoming}.market_instance_id,{incoming}.selection_id,{incoming}.is_main,{incoming}.score_basis) "
        f"OR {incoming}.source_updated_at IS NULL AND {current}.received_at IS DISTINCT FROM {incoming}.received_at)"
    )


QUOTE_FIELDS = (
    'market_name', 'market_kind', 'line', 'price', 'source_updated_at',
    'received_at', 'source_time_quality', 'status', 'play_id',
    'market_instance_id', 'selection_id', 'is_main', 'score_basis',
)


def _same_reference(stored: str, incoming: str) -> str:
    return (
        f'{stored}.fixture_id={incoming}.fixture_id '
        f'AND {stored}.market_id={incoming}.market_id '
        f'AND {stored}.period={incoming}.period '
        f'AND coalesce({stored}.line,0)=coalesce({incoming}.line,0) '
        f'AND {stored}.selection={incoming}.selection '
    )


async def upsert_quotes(conn, payload):
    # Caller holds fixture state locks and rejects duplicate normalized keys.
    # Locate each reference once. Materialize its stable id and update need,
    # then recheck the live row after any concurrent UPDATE wait. Missing
    # references still use ON CONFLICT to revalidate a concurrent insertion.
    # Fixed field/alias constants build SQL; every incoming value is bound.
    fields = ('id', 'fixture_id', 'market_id', 'period', 'selection', *QUOTE_FIELDS)
    updates = ','.join(f'{field}=q.{field}' for field in QUOTE_FIELDS)
    conflicts = ','.join(f'{field}=EXCLUDED.{field}' for field in QUOTE_FIELDS)
    counts = Counter(row['fixture_id'] for row in payload)
    dense = counts and min(counts.values()) >= MIN_PREFETCH_QUOTES_PER_FIXTURE
    # Dense snapshots otherwise repeat the same fixture's index traversal for
    # every selection. Keep sparse WS updates on the exact-reference path;
    # fetching a whole fixture costs more than those few point lookups.
    prefetch = (
        "current_rows AS MATERIALIZED (SELECT stored.* FROM v2_current_quotes stored "
        "JOIN (SELECT DISTINCT fixture_id FROM incoming) selected USING(fixture_id)), "
        if dense else ""
    )
    stored_relation = 'current_rows' if dense else 'v2_current_quotes'
    await conn.execute(
        "WITH incoming AS MATERIALIZED ("
        "SELECT * FROM jsonb_to_recordset($1::jsonb) AS q("
        "id uuid,fixture_id text,market_id text,market_name text,period text,"
        "market_kind text,line numeric,selection text,price numeric,"
        "source_updated_at timestamptz,received_at timestamptz,source_time_quality text,"
        "status text,play_id text,market_instance_id text,selection_id text,"
        "is_main boolean,score_basis text)), " + prefetch + "matched AS MATERIALIZED ("
        "SELECT q.*,stored.id AS existing_id,("
        + quote_update_condition('stored', 'q') + ") AS needs_update "
        "FROM incoming q LEFT JOIN " + stored_relation + " stored ON "
        + _same_reference('stored', 'q') + "), updated AS ("
        "UPDATE v2_current_quotes stored SET " + updates + " FROM matched q WHERE "
        "q.needs_update AND stored.id=q.existing_id AND (" + quote_update_condition('stored', 'q')
        + ") RETURNING stored.id) "
        "INSERT INTO v2_current_quotes(" + ','.join(fields) + ") "
        "SELECT " + ','.join('q.' + field for field in fields) + " FROM matched q "
        "WHERE q.existing_id IS NULL "
        "ON CONFLICT(fixture_id,market_id,period,(COALESCE(line,0)),selection) "
        "DO UPDATE SET " + conflicts + " WHERE "
        + quote_update_condition('v2_current_quotes', 'EXCLUDED'),
        payload,
    )
