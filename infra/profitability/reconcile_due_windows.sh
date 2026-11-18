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
    first_incomplete = None
    for entry in archive:
        if first_incomplete is not None:
            continue
        try:
            report = json.load(archive.extractfile(entry))
        except (ValueError, UnicodeError):
            report = None
        if not (isinstance(report, list) and len(report) == 1
                and isinstance(report[0], dict) and report[0].get("complete_page") is True):
            first_incomplete = entry.name.removesuffix(".json")
    if first_incomplete is not None:
        print(first_incomplete)
')
    suffix=.retry.json
fi

[ -n "$ordinal" ] || exit 0
start_us=$((anchor + ordinal * window_us))
start_seconds=$((start_us / 1000000))
start_fraction=$(printf '%06d' "$((start_us % 1000000))")
start=$(date -u -d "@$start_seconds.$start_fraction" +%Y-%m-%dT%H:%M:%S.%6NZ)
draft=$(mktemp -- "$state_dir/.rolling.XXXXXX")
trap 'rm -f -- "$draft"' EXIT
bash "$root/reconcile_live_window.sh" "$start" > "$draft"
test -s "$draft"
mv -T -- "$draft" "$state_dir/$ordinal$suffix"
printf 'Recorded window %s starting at %s in %s\n' "$ordinal" "$start" "$ordinal$suffix"
