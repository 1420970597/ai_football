#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# != 2 || "$1" != *Z ]]; then
    printf '%s\n' 'Usage: collect_preflight_cohorts.sh <UTC cohort start ending in Z> <private archive directory>' >&2
    exit 2
fi

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
start=$(date -u -d "$1" +%Y-%m-%dT%H:%M:%SZ)
follow_up_end=$(date -u -d "$start 120 hours" +%Y-%m-%dT%H:%M:%SZ)
now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
if [[ "$now" < "$start" ]]; then
    exit 0
fi
install -d -m 700 -- "$2"
archive=$(cd -- "$2" && pwd)
exec 9>"$archive/.collect.lock"
flock -n 9 || exit 0
target="$archive/$now.json"
final_sample="$archive/final.json"
if [[ -s "$target" || -s "$final_sample" ]]; then
    exit 0
fi

draft=$(mktemp -- "$archive/.capture.XXXXXX")
trap 'rm -f -- "$draft"' EXIT
docker exec -i ai-football-v2-api-1 python -c '
import asyncio
from datetime import datetime, timedelta
import hashlib
import sys

import asyncpg
from app.config import database_url


async def collect(start, document):
    query = (document.split("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;", 1)[1].strip()
             .removesuffix("ROLLBACK;").replace(":\x27begin\x27", "$1")
             .replace("nullif(:\x27end\x27,\x27\x27)", "$2"))
    interval = timedelta(hours=24)
    middle = start + interval
    end = middle + interval
    conn = await asyncpg.connect(database_url(), timeout=5, command_timeout=10)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            bindings = await conn.fetchrow("""
                SELECT count(DISTINCT (account_id, provider_identity_hash)),
                       count(*) FILTER (WHERE account_id IS NULL OR provider_identity_hash IS NULL)
                FROM v2_bet_orders WHERE provider_order_no IS NOT NULL
                """)
            if tuple(bindings.values()) != (1, 0):
                raise ValueError("unique_ledger_binding_required")
            first = await conn.fetchval(query, start, middle)
            second = await conn.fetchval(query, middle, end)
            return await conn.fetchval("""
                SELECT jsonb_build_object(
                    \x27observed_at\x27, now(), \x27start\x27, $1::timestamptz, \x27split\x27, $2::timestamptz,
                    \x27end\x27, $3::timestamptz, \x27minimum_follow_up_end\x27, $3 + interval \x2772 hours\x27,
                    \x27query_sha256\x27, $4::text, \x27cohorts\x27, jsonb_build_array($5::jsonb, $6::jsonb))
                """, start, middle, end, hashlib.sha256(document.encode()).hexdigest(), first, second)
    finally:
        await conn.close()


try:
    print(asyncio.run(collect(datetime.fromisoformat(sys.argv[1]), sys.stdin.read())))
except Exception as exc:
    raise SystemExit(type(exc).__name__) from None
' "$start" < "$root/snapshot_fill_comparison.sql" > "$draft"
test -s "$draft"
ln -T -- "$draft" "$target"
if [[ ! "$now" < "$follow_up_end" ]]; then
    ln -T -- "$draft" "$final_sample"
fi
printf 'Recorded preflight cohort snapshot at %s\n' "$now"
