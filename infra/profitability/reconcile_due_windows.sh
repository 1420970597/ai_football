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
state_dir=$(cd -- "$state_dir" && pwd)
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
snapshot_dir="$state_dir/../state"
snapshot_member=()
if [ -s "$snapshot_dir/latest.json" ]; then
    snapshot_member=(-C "$snapshot_dir" latest.json)
fi
for ((index=0; index<=latest_due; index++)); do
    if [ ! -s "$state_dir/$index.json" ]; then
        ordinal=$index
        break
    fi
    if [ ! -s "$state_dir/$index.retry.json" ]; then
        candidates+=("$index.json")
    elif ((${#snapshot_member[@]} && index>latest_due-5)); then
        candidates+=("$index.json" "$index.retry.json")
    fi
done

suffix=.json
if [ -z "$ordinal" ] && ((${#candidates[@]})); then
    checks=$(tar -cf - "${snapshot_member[@]}" -C "$state_dir" -- "${candidates[@]}" |
        docker exec -i ai-football-v2-executor-1 python -c '
from datetime import datetime, timedelta, timezone
import json
import sys
import tarfile

def window_report(data):
    return data[0] if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) else {}

def requires_retry(report):
    if report.get("complete_page") is not True or report.get("local_only") != 0 or report.get("upstream_only") != 0:
        return True
    discrepancies = {
        "stake_difference", "payout_difference", "net_profit_difference", "currency_difference", "fees_difference",
        "placement_time_difference", "settlement_time_difference", "outside_settlement_window",
        "upstream_settlement_before_placement",
    }
    issues = report.get("issues", {})
    return not isinstance(issues, dict) or any(issues.get(key) for key in discrepancies)

def matches_bounds(item, start_key, end_key, start, end):
    try:
        return datetime.fromisoformat(item[start_key]) == start and datetime.fromisoformat(item[end_key]) == end
    except (KeyError, TypeError, ValueError):
        return False

anchor = datetime.fromisoformat(sys.argv[1])
seconds, micros = divmod(int(sys.argv[2]), 1_000_000)
now = datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(microseconds=micros)
latest_due = int(sys.argv[3])
reports = {}
snapshot = None
with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as archive:
    for entry in archive:
        try:
            data = json.load(archive.extractfile(entry))
        except (ValueError, UnicodeError):
            data = None
        if entry.name == "latest.json":
            snapshot = data
        else:
            reports[entry.name] = window_report(data)

for name, report in reports.items():
    if name.endswith(".retry.json"):
        continue
    index = name.removesuffix(".json")
    if requires_retry(report) and f"{index}.retry.json" not in reports:
        print(index, "retry")
        break
else:
    try:
        observed = datetime.fromisoformat(snapshot["observed_at"])
        valid = datetime.fromisoformat(snapshot["anchor"]) == anchor and (
            now - timedelta(minutes=11) <= observed <= now
        )
        windows = snapshot["windows"] if valid else []
    except (KeyError, TypeError, ValueError):
        windows = []
    for window in windows if isinstance(windows, list) else []:
        if not isinstance(window, dict):
            continue
        index, settled = window.get("ordinal"), window.get("settled")
        if type(index) is not int or type(settled) is not int or window.get("closed") is not True:
            continue
        if not max(0, latest_due - 4) <= index <= latest_due:
            continue
        start = anchor + timedelta(hours=index * 12)
        end = start + timedelta(hours=12)
        if not matches_bounds(window, "since", "until", start, end):
            continue
        if window.get("accounts") != 1 or window.get("provider_accounts") != 1:
            continue
        original = reports.get(f"{index}.json", {})
        retry = reports.get(f"{index}.retry.json", {})
        counts = [r.get("local_settled") for r in (original, retry)
                  if matches_bounds(r, "begin", "end", start, end)]
        counts = [count for count in counts if type(count) is int and count >= 0]
        if counts and settled > max(counts):
            print(index, f"late-{settled}")
' "$1" "$now" "$latest_due")
    while read -r index check; do
        if [ -n "$index" ] && [ ! -s "$state_dir/$index.$check.json" ]; then
            ordinal=$index
            suffix=".$check.json"
            break
        fi
    done <<< "$checks"
fi

if [ -n "$ordinal" ]; then
    record_report "$ordinal" "$suffix" reconcile_live_window.sh
fi
exit "$wallet_status"
