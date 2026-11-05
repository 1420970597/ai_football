"""Stable quote identities: update changed rows; insert missing rows with conflict revalidation."""

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
    # Fixed field/alias constants build SQL; every incoming value is bound.
    fields = ('id', 'fixture_id', 'market_id', 'period', 'selection', *QUOTE_FIELDS)
    updates = ','.join(f'{field}=q.{field}' for field in QUOTE_FIELDS)
    conflicts = ','.join(f'{field}=EXCLUDED.{field}' for field in QUOTE_FIELDS)
    await conn.execute(
        "WITH incoming AS MATERIALIZED ("
        "SELECT * FROM jsonb_to_recordset($1::jsonb) AS q("
        "id uuid,fixture_id text,market_id text,market_name text,period text,"
        "market_kind text,line numeric,selection text,price numeric,"
        "source_updated_at timestamptz,received_at timestamptz,source_time_quality text,"
        "status text,play_id text,market_instance_id text,selection_id text,"
        "is_main boolean,score_basis text)), updated AS ("
        "UPDATE v2_current_quotes stored SET " + updates + " FROM incoming q WHERE "
        + _same_reference('stored', 'q') + "AND (" + quote_update_condition('stored', 'q')
        + ") RETURNING stored.id) "
        "INSERT INTO v2_current_quotes(" + ','.join(fields) + ") "
        "SELECT " + ','.join('q.' + field for field in fields) + " FROM incoming q "
        "WHERE NOT EXISTS(SELECT 1 FROM v2_current_quotes stored WHERE "
        + _same_reference('stored', 'q') + ") "
        "ON CONFLICT(fixture_id,market_id,period,(COALESCE(line,0)),selection) "
        "DO UPDATE SET " + conflicts + " WHERE "
        + quote_update_condition('v2_current_quotes', 'EXCLUDED'),
        payload,
    )
