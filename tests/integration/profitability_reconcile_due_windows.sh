#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
test_dir=$(mktemp -d)
trap 'rm -rf -- "$test_dir"' EXIT
cd "$test_dir"
mkdir state
cp "$root/infra/profitability/reconcile_due_windows.sh" .

date() {
    if [[ "$*" == '-u +%s%6N' ]]; then
        printf '%s\n' "$test_now"
    else
        command date "$@"
    fi
}
docker() {
    [[ "$1 $2 $3 $4 $5" == 'exec -i ai-football-v2-executor-1 python -c' ]] || return 2
    python "${@:5}"
}
export -f date docker
printf '%s\n' 'set -eu' 'printf "%s\n" "$1" >> calls' \
    'if [[ ${test_fail:-0} == 1 ]]; then exit 41; fi' \
    'printf "%s\n" "$test_report"' > reconcile_live_window.sh
printf '%s\n' 'set -eu' 'printf "%s\n" "$1" >> wallet_calls' \
    'if [[ ${test_wallet_fail:-0} == 1 ]]; then exit 42; fi' \
    'printf "%s\n" "{\"samples\":0}"' > reconcile_wallet_window.sh

anchor=2026-11-18T02:14:09.845374Z
base=$(date -u -d "$anchor" +%s%6N)
window_us=43200000000
delay_us=350000000
run() {
    bash ./reconcile_due_windows.sh "$anchor" "$test_dir/state"
}

# An empty placeholder is eligible only after the delay.
export test_now=$((base + window_us + delay_us - 1))
export test_report='[{"complete_page": false, "scan_issue": "upstream_total_changed"}]'
: > state/0.json
run
test ! -e calls
test ! -e wallet_calls
export test_now=$((test_now + 1))
export test_wallet_fail=1
result=0
run || result=$?
test "$result" = 42
test "$(sed -n '1p' calls)" = "$anchor"
test "$(sed -n '1p' wallet_calls)" = "$anchor"
test ! -e state/0.wallet.json
cp state/0.json original

# An unsuccessful attempt is retried and the original report survives.
export test_fail=1
export test_wallet_fail=0
result=0
run || result=$?
test "$result" = 41
test "$(stat -c %a state/0.wallet.json)" = 600
test "$(sed -n '2p' wallet_calls)" = "$anchor"
test ! -e state/0.retry.json
test -z "$(find state -name '.rolling.??????' -print)"
cmp state/0.json original
export test_fail=0
export test_report='[{"complete_page": true, "order_receipt_fields_reconciled": false, "local_only": 0, "upstream_only": 0, "issues": {"missing_currency": 1, "missing_fees": 1}}]'
run
test "$(stat -c %a state/0.retry.json)" = 600
cmp state/0.json original
run
test "$(wc -l < calls)" = 3
test "$(wc -l < wallet_calls)" = 2

# Missing reports come first, then malformed reports. Missing receipt fields do not cause a retry.
export test_now=$((base + 4 * window_us + delay_us))
printf '%s\n' "$test_report" > state/1.json
printf '%s\n' '{broken' > state/2.json
run
test "$(sed -n '4p' calls)" = 2026-11-19T14:14:09.845374Z
test ! -e state/2.retry.json
run
test "$(sed -n '5p' calls)" = 2026-11-19T02:14:09.845374Z
test -s state/2.retry.json
test ! -e state/1.retry.json
test "$(cat state/2.json)" = '{broken'
run
test "$(wc -l < calls)" = 5

export test_now=$((base + 5 * window_us + delay_us))
run
test "$(sed -n '6p' calls)" = 2026-11-20T02:14:09.845374Z
test -s state/4.json
test -s state/4.wallet.json
run
test "$(wc -l < calls)" = 6

# A complete scan with a different order list gets one retry in either direction.
for direction in local_only upstream_only; do
    if [ "$direction" = local_only ]; then
        export test_report='[{"complete_page": true, "local_only": 1, "upstream_only": 0}]'
        ordinal=5
    else
        export test_report='[{"complete_page": true, "local_only": 0, "upstream_only": 1}]'
        ordinal=6
    fi
    export test_now=$((base + (ordinal + 1) * window_us + delay_us))
    run
    cp "state/$ordinal.json" original
    run
    cmp "state/$ordinal.json" original
    test -s "state/$ordinal.retry.json"
    run
    test "$(wc -l < calls)" = "$((6 + 2 * (ordinal - 4)))"
done
printf '%s\n' 'Reconciliation scheduling checks passed.'
