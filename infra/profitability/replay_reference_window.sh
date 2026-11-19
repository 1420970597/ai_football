#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# != 5 || ! "$1" =~ ^[a-z0-9-]{1,160}$ || ! "$2" =~ ^leyu:[0-9]+$ ]]; then
    printf '%s\n' 'Usage: replay_reference_window.sh <archive-label> <fixture-id> <UTC begin> <UTC end> <archive bindings filename>' >&2
    exit 2
fi
label=$1
fixture=$2
begin=$3
end=$4
bindings=$5
if [[ ! "$bindings" =~ ^[a-z0-9-]+\.json$ ]]; then
    exit 2
fi
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
archive="$root/.runtime/profitability-watch/public-orderbook-pilot-20261118"
test -r "$archive/$bindings"
shopt -s nullglob
captures=("$archive/$label-"*.jsonl)
if (( ${#captures[@]} == 0 )); then
    printf '%s\n' 'No order book archive for this fixture' >&2
    exit 1
fi
draft=$(mktemp -- "$archive/.reference-replay.XXXXXX")
trap 'rm -f -- "$draft"' EXIT
database_rows=$(docker exec -i ai-football-v2-api-1 python -c '
import asyncio
from datetime import datetime, timedelta, timezone
import sys

import asyncpg
from app.config import database_url

async def read():
    sql = sys.stdin.read()
    begin, end = (datetime.fromisoformat(value) for value in sys.argv[2:])
    if not (begin.utcoffset() == end.utcoffset() == timedelta(0)
            and timedelta(0) < end - begin <= timedelta(hours=8) and end <= datetime.now(timezone.utc)):
        raise ValueError("invalid preflight bounds")
    conn = await asyncpg.connect(database_url(), timeout=5, command_timeout=10)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            rows = await conn.fetchval(sql, sys.argv[1], begin, end)
            if rows is None:
                raise ValueError("preflight account or fixture scope unavailable")
            print(rows)
    finally:
        await conn.close()

try:
    asyncio.run(read())
except Exception as error:
    raise SystemExit(type(error).__name__) from None
' "$fixture" "$begin" "$end" < "$root/infra/profitability/reference_preflights.sql")
reference_files=()
for capture in "${captures[@]}"; do
    reference_files+=("/evidence/$(basename -- "$capture")")
done
research="$root/reports/profitability-pilot-20261118"
outcomes=()
if [[ "$bindings" == fixture-bindings-*.json ]]; then
    outcome_file="reference-outcomes-${bindings#fixture-bindings-}"
    if [[ -r "$research/$outcome_file" ]]; then
        outcomes=(--outcomes "/research/$outcome_file")
    fi
fi
printf '%s\n' "$database_rows" | docker run --rm -i --user 0:0 --network none --cpus .25 --memory 128m \
    --entrypoint python -v "$research:/research:ro" \
    -v "$archive:/evidence:ro" ai-football-verifier:v2.0.0-dev /research/reference_preflight.py \
    /dev/stdin "/evidence/$bindings" "${reference_files[@]}" "${outcomes[@]}" > "$draft"
target="$archive/$label-replay-$(date -u +%FT%TZ).json"
ln -T -- "$draft" "$target"
printf 'Archived preflight replay in %s\n' "$(basename -- "$target")"
