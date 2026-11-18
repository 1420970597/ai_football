#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
test -n "${DATABASE_URL:-}"
case "$DATABASE_URL" in
    postgresql://postgres@ai-football-preflight-test-db/postgres) ;;
    *) exit 2 ;;
esac
test_dir=$(mktemp -d)
trap 'rm -rf -- "$test_dir"' EXIT
archive="$test_dir/archive"
start=2026-01-01T00:00:00Z

date() {
    if [[ "$*" == '-u +%Y-%m-%dT%H:%M:%SZ' ]]; then
        printf '%s\n' "$test_now"
    else
        command date "$@"
    fi
}

docker() {
    [[ "$1 $2 $3 $4 $5" == 'exec -i ai-football-v2-api-1 python -c' ]] || return 2
    if [[ ${test_fail:-0} == 1 ]]; then
        return 41
    fi
    python "${@:5}"
}

export -f date docker
run() {
    bash "$root/infra/profitability/collect_preflight_cohorts.sh" "$start" "$archive"
}

export test_now=2025-12-31T23:59:59Z
run
test ! -d "$archive"

export test_now=2026-01-01T00:00:00Z test_fail=1
status=0
run || status=$?
test "$status" = 41
test ! -e "$archive/$test_now.json"
test -z "$(find "$archive" -name '.capture.??????' -print)"

export test_fail=0
run
test "$(stat -c %a "$archive")" = 700
test "$(stat -c %a "$archive/$test_now.json")" = 600
sha256sum "$archive/$test_now.json"
original=$(sha256sum "$archive/$test_now.json")
test_fail=1 run
test "$(sha256sum "$archive/$test_now.json")" = "$original"
python -c '
from datetime import datetime,timedelta
import json,sys

data=json.load(sys.stdin)
start,middle,end,follow_up=(datetime.fromisoformat(data[k]) for k in ("start","split","end","minimum_follow_up_end"))
assert middle-start == end-middle == timedelta(days=1)
assert follow_up-end == timedelta(days=3)
assert len(data["query_sha256"]) == 64
first,second=data["cohorts"]
assert datetime.fromisoformat(first["bounds"]["begin_at"])==start
assert datetime.fromisoformat(second["bounds"]["begin_at"])==middle
assert first["bounds"]["observed_at"]==second["bounds"]["observed_at"]==data["observed_at"]
assert first["comparison"][0]["fills"]==12 and second["comparison"][0]["fills"]==0
automatic, live = first["automatic_coverage"]
assert automatic["phase"]=="all" and live["phase"]=="live"
assert automatic["fills"]==11 and automatic["comparable_fills"]==1 and automatic["excluded_fills"]==10
assert automatic["excluded_settled"]==10 and automatic["excluded_receipt_net"]==55
assert automatic["excluded_missing_cost_fields"]==10 and automatic["excluded_missing_receipt_net"]==0
assert second["automatic_coverage"][0]["fills"]==0 and second["automatic_coverage"][0]["excluded_receipt_net"] is None
' < "$archive/$test_now.json"

export test_now=2026-01-05T23:59:59Z
run
test ! -e "$archive/final.json"
export test_now=2026-01-06T00:00:00Z
run
test "$archive/$test_now.json" -ef "$archive/final.json"
export test_now=2026-01-06T01:00:00Z
test_fail=1 run
test ! -e "$archive/$test_now.json"
