#!/usr/bin/env bash
set -euo pipefail
umask 077

script=reference_orderbooks.py
archive_name=public-orderbook-pilot-20261118
if [[ ${1-} == --stream ]]; then
    script=reference_orderbook_stream.py
    archive_name=public-orderbook-stream-20261119
    shift
elif [[ ${1-} == --score ]]; then
    script=reference_basketball_state.py
    archive_name=public-score-pilot-20261119
    shift
fi
if [[ $# -lt 2 || ! "$1" =~ ^[a-z0-9-]{1,160}$ ]]; then
    printf '%s\n' 'Usage: capture_reference_window.sh [--stream|--score] <archive-label> <source arguments>' >&2
    exit 2
fi
label=$1
shift
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
archive="$root/.runtime/profitability-watch/$archive_name"
install -d -m 700 -- "$archive"
target="$archive/$label-$(date -u +%FT%TZ).jsonl"
set -o noclobber
docker run --rm --network host --cpus .25 --memory 128m --entrypoint python \
    -v "$root/reports/profitability-pilot-20261118:/research:ro" \
    ai-football-verifier:v2.0.0-dev "/research/$script" "$@" > "$target"
printf 'Archived %s observations in %s\n' "$(wc -l < "$target")" "$(basename -- "$target")"
