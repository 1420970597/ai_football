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

# The first four windows have their own timers. Once the fifth is due, this
# also catches up any earlier windows whose timers left no report.
if (( latest_due < 4 )); then
    exit 0
fi

install -d -m 700 -- "$state_dir"
exec 9>"$state_dir/.rolling.lock"
flock -n 9 || exit 0

for ((ordinal=0; ordinal<=latest_due; ordinal++)); do
    report="$state_dir/$ordinal.json"
    if [ -s "$report" ]; then
        continue
    fi
    start_us=$((anchor + ordinal * window_us))
    start_seconds=$((start_us / 1000000))
    start_fraction=$(printf '%06d' "$((start_us % 1000000))")
    start=$(date -u -d "@$start_seconds.$start_fraction" +%Y-%m-%dT%H:%M:%S.%6NZ)
    draft=$(mktemp -- "$state_dir/.rolling.XXXXXX")
    trap 'rm -f -- "$draft"' EXIT
    bash "$root/reconcile_live_window.sh" "$start" > "$draft"
    test -s "$draft"
    mv -T -- "$draft" "$report"
    printf 'Reconciled window %s starting at %s\n' "$ordinal" "$start"
    break
done
