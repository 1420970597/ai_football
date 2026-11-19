#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
    printf '%s\n' 'Usage: reconcile_live_window.sh <UTC start of a closed 12-hour window>' >&2
    exit 2
fi

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
checker=$(base64 -w0 "$root/reconcile_upstream.py")

docker exec -i ai-football-v2-api-1 python - "$1" <<'PY' |
import asyncio
from datetime import datetime, timedelta, timezone
import json
import sys

import asyncpg
from app.account_jobs import decode_document, scope
from app.config import load_settings


async def read_window():
    begin = datetime.fromisoformat(sys.argv[1])
    if begin.utcoffset() != timedelta(0):
        raise ValueError("utc_start_required")
    end = begin + timedelta(hours=12)
    if end > datetime.now(timezone.utc):
        raise ValueError("window_open")

    settings = load_settings()
    conn = await asyncpg.connect(settings.database_url, timeout=5, command_timeout=5)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            accounts = await conn.fetch("""
                WITH bindings AS (
                    SELECT DISTINCT account_id, provider_identity_hash
                    FROM v2_bet_orders WHERE account_id IS NOT NULL AND provider_order_no IS NOT NULL
                )
                SELECT a.id, a.user_id, a.config_revision, a.encrypted_session, a.provider_identity_hash
                FROM v2_leyu_accounts a JOIN bindings b
                    ON a.id = b.account_id AND a.provider_identity_hash = b.provider_identity_hash
                WHERE a.health_state = 'healthy' AND a.encrypted_session IS NOT NULL
                    AND (SELECT count(*) FROM bindings) = 1
            """)
            if len(accounts) != 1:
                raise ValueError("unique_ledger_account_unavailable")
            account = accounts[0]
            rows = await conn.fetch("""
                SELECT provider_order_no AS order_no, provider_stake AS stake, provider_payout AS payout,
                       settled_net AS net_profit, provider_fees AS fees, provider_currency AS currency,
                       settled_at, coalesce(settled_at, receipt_observed_at) AS booked_at,
                       provider_placed_at AS placed_at, receipt_observed_at,
                       pnl_basis, result_payload->'provider_settlement_codes' AS settlement_codes,
                       encode(sha256(convert_to(jsonb_build_array(
                           o.account_id, o.provider_identity_hash, o.provider_order_no,
                           o.provider_stake, o.provider_payout, o.settled_net, o.provider_fees, o.provider_currency, o.pnl_basis,
                           extract(epoch FROM o.provider_placed_at), extract(epoch FROM o.settled_at),
                           extract(epoch FROM o.receipt_observed_at))::text,'UTF8')),'hex') AS receipt_row_sha256,
                       CASE WHEN settled_at IS NULL THEN (
                           SELECT min(e.observed_at) FROM v2_order_receipt_evidence e
                           WHERE e.order_id = o.id AND e.payload->>'state' = 'settled'
                             AND e.payload->>'order_no' = o.provider_order_no
                       ) END AS first_settled_observed_at
                FROM v2_bet_orders o
                WHERE account_id=$1 AND provider_identity_hash=$2 AND state='settled'
                  AND coalesce(settled_at,receipt_observed_at)>=$3
                  AND coalesce(settled_at,receipt_observed_at)<$4
                ORDER BY o.id::text COLLATE "C"
            """, account["id"], account["provider_identity_hash"], begin, end)
            session = decode_document(settings.encryption_key, account["encrypted_session"])
            base = session["leyu_api_base"]
            return dict(start=begin.isoformat(), windows=1, scope=scope(account), base_url=base,
                        request_id=session["leyu_request_id"], origin=session.get("leyu_origin") or base,
                        local=[dict(row) for row in rows])
    finally:
        await conn.close()


try:
    print(json.dumps(asyncio.run(read_window()), default=str))
except Exception as exc:
    raise SystemExit(type(exc).__name__) from None
PY
docker exec -i ai-football-v2-executor-1 python -c \
    'import base64, sys; exec(compile(base64.b64decode(sys.argv[1]), "reconcile_upstream.py", "exec"))' "$checker"
