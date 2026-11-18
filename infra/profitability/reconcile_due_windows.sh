#!/usr/bin/env bash
set -euo pipefail
umask 077

if [ "$#" -ne 2 ]; then
    printf '%s\n' 'Usage: reconcile_due_windows.sh <fixed UTC anchor> <private reconciliation directory>' >&2
    exit 2
fi

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
anchor=$(date -u -d "$1" +%s%6N)
now=$(date -u +%s%6N)
state_dir=$2
window_us=43200000000
delay_us=350000000
latest_due=$(((now - anchor - delay_us) / window_us - 1))

if (( latest_due < 0 )); then
    exit 0
fi

install -d -m 700 -- "$state_dir"
exec 9>"$state_dir/.rolling.lock"
flock -n 9 || exit 0

record_report() (
    ordinal=$1
    suffix=$2
    collector=$3
    start_us=$((anchor + ordinal * window_us))
    start_seconds=$((start_us / 1000000))
    start_fraction=$(printf '%06d' "$((start_us % 1000000))")
    start=$(date -u -d "@$start_seconds.$start_fraction" +%Y-%m-%dT%H:%M:%S.%6NZ) || exit
    draft=$(mktemp -- "$state_dir/.rolling.XXXXXX") || exit
    trap 'rm -f -- "$draft"' EXIT
    bash "$root/$collector" "$start" > "$draft" || exit
    test -s "$draft" || exit
    mv -T -- "$draft" "$state_dir/$ordinal$suffix" || exit
    printf 'Recorded window %s starting at %s in %s\n' "$ordinal" "$start" "$ordinal$suffix"
)

# A full missing wallet period needs its oldest samples to be within the 72-hour retention.
wallet_status=0
for ((index=latest_due; index>=0 && index>latest_due-5; index--)); do
    if [ ! -s "$state_dir/$index.wallet.json" ]; then
        record_report "$index" .wallet.json reconcile_wallet_window.sh || wallet_status=$?
        break
    fi
done

ordinal=
candidates=()
for ((index=0; index<=latest_due; index++)); do
    if [ ! -s "$state_dir/$index.json" ]; then
        ordinal=$index
        break
    fi
    if [ ! -s "$state_dir/$index.retry.json" ]; then
        candidates+=("$index.json")
    fi
done

suffix=.json
if [ -z "$ordinal" ] && ((${#candidates[@]})); then
    ordinal=$(tar -C "$state_dir" -cf - -- "${candidates[@]}" |
        docker exec -i ai-football-v2-executor-1 python -c '
import json
import sys
import tarfile

with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as archive:
    first_unmatched = None
    for entry in archive:
        if first_unmatched is not None:
            continue
        try:
            report = json.load(archive.extractfile(entry))
        except (ValueError, UnicodeError):
            report = None
        if not (isinstance(report, list) and len(report) == 1
                and isinstance(report[0], dict) and report[0].get("complete_page") is True
                and report[0].get("local_only") == report[0].get("upstream_only") == 0):
            first_unmatched = entry.name.removesuffix(".json")
    if first_unmatched is not None:
        print(first_unmatched)
')
    suffix=.retry.json
fi

if [ -n "$ordinal" ]; then
    record_report "$ordinal" "$suffix" reconcile_live_window.sh
fi
exit "$wallet_status"
