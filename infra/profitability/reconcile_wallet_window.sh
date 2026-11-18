#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
    printf '%s\n' 'Usage: reconcile_wallet_window.sh <UTC start of a closed 12-hour window>' >&2
    exit 2
fi

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
begin=$(date -u -d "$1" +%Y-%m-%dT%H:%M:%S.%6NZ)
end=$(date -u -d "$begin 12 hours" +%Y-%m-%dT%H:%M:%S.%6NZ)
if [[ "$end" > "$(date -u +%Y-%m-%dT%H:%M:%S.%6NZ)" ]]; then
    printf '%s\n' 'Wallet window is still open.' >&2
    exit 2
fi

(
    printf '\\set wallet_samples '
    docker exec ai-football-v2-profitability-watch tar -C /state -cf - wallet-observations |
        docker exec -i ai-football-v2-executor-1 python -c '
import base64
from decimal import Decimal
import json
import sys
import tarfile

with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as files:
    samples = [json.load(files.extractfile(item), parse_float=Decimal) for item in files if item.isfile()]
print(base64.b64encode(json.dumps(samples, default=str).encode()).decode())
' || exit
    cat "$root/wallet_cash_flow.sql"
) | docker exec -i \
    -e 'PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=8000 -c lock_timeout=1000 -c TimeZone=UTC' \
    ai-football-v2-profitability-watch psql -XqAt -v ON_ERROR_STOP=1 \
    -v "window_start=$begin" -v "window_end=$end"
