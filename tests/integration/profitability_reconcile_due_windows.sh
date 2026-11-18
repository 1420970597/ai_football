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

# Recheck recent windows only when a fresh fixed-window snapshot gains receipts.
mkdir -p watch/reconciliations watch/state
state_path="$test_dir/watch/reconciliations"
export test_now=$((base + window_us + delay_us))
export test_report='[{"complete_page":true,"local_only":0,"upstream_only":0,"local_settled":51,"begin":"2026-11-18T02:14:09.845374Z","end":"2026-11-18T14:14:09.845374Z"}]'
bash ./reconcile_due_windows.sh "$anchor" "$state_path"
cp "$state_path/0.json" original_late
calls_before=$(wc -l < calls)
snapshot() {
    printf '{"anchor":"%s","observed_at":"%s","windows":[{"ordinal":0,"since":"%s","until":"%s","closed":true,"settled":%s,"accounts":%s,"provider_accounts":1}]}\n' \
        "$1" "$2" "$anchor" "$3" "$4" "${5:-1}" > watch/state/latest.json
}
boundary=2026-11-18T14:14:09.845374Z
fresh=2026-11-18T14:19:58Z
snapshot "$anchor" "$fresh" "$boundary" 51
bash ./reconcile_due_windows.sh "$anchor" "$state_path"
snapshot 2026-11-18T02:14:00Z "$fresh" "$boundary" 52
bash ./reconcile_due_windows.sh "$anchor" "$state_path"
snapshot "$anchor" 2026-11-18T14:00:00Z "$boundary" 52
bash ./reconcile_due_windows.sh "$anchor" "$state_path"
snapshot "$anchor" "$fresh" 2026-11-18T14:14:00Z 52
bash ./reconcile_due_windows.sh "$anchor" "$state_path"
snapshot "$anchor" "$fresh" "$boundary" 52 2
bash ./reconcile_due_windows.sh "$anchor" "$state_path"
test "$(wc -l < calls)" = "$calls_before"
snapshot "$anchor" "$fresh" "$boundary" 52
export test_fail=1
result=0
bash ./reconcile_due_windows.sh "$anchor" "$state_path" || result=$?
test "$result" = 41
test ! -e "$state_path/0.late-52.json"
export test_fail=0
export test_report='[{"complete_page":true,"local_only":0,"upstream_only":0,"local_settled":52}]'
bash ./reconcile_due_windows.sh "$anchor" "$state_path"
test "$(stat -c %a "$state_path/0.late-52.json")" = 600
bash ./reconcile_due_windows.sh "$anchor" "$state_path"
test "$(wc -l < calls)" = "$((calls_before + 2))"
snapshot "$anchor" "$fresh" "$boundary" 53
bash ./reconcile_due_windows.sh "$anchor" "$state_path"
test -s "$state_path/0.late-53.json"
cmp "$state_path/0.json" original_late
printf '%s\n' 'Late settlement reconciliation checks passed.'

# Both previous scans can miss upstream orders while the local ledger advances.
state_path="$test_dir/watch/unmatched"
calls_before=$(wc -l < calls)
snapshot "$anchor" "$fresh" "$boundary" 51
export test_report='[{"complete_page":false,"local_only":2,"upstream_only":0,"local_settled":50,"begin":"2026-11-18T02:14:09.845374Z","end":"2026-11-18T14:14:09.845374Z"}]'
bash ./reconcile_due_windows.sh "$anchor" "$state_path"
cp "$state_path/0.json" unmatched_original
export test_report='[{"complete_page":true,"local_only":1,"upstream_only":0,"local_settled":51,"begin":"2026-11-18T02:14:09.845374Z","end":"2026-11-18T14:14:09.845374Z"}]'
bash ./reconcile_due_windows.sh "$anchor" "$state_path"
cp "$state_path/0.retry.json" unmatched_retry
bash ./reconcile_due_windows.sh "$anchor" "$state_path"
test "$(wc -l < calls)" = "$((calls_before + 2))"
snapshot "$anchor" "$fresh" "$boundary" 52
export test_report='[{"complete_page":true,"local_only":0,"upstream_only":0,"local_settled":52}]'
bash ./reconcile_due_windows.sh "$anchor" "$state_path"
test -s "$state_path/0.late-52.json"
bash ./reconcile_due_windows.sh "$anchor" "$state_path"
test "$(wc -l < calls)" = "$((calls_before + 3))"
cmp "$state_path/0.json" unmatched_original
cmp "$state_path/0.retry.json" unmatched_retry

mkdir -p infra/profitability .runtime/profitability-watch/reconciliations
cp "$root/infra/profitability/restore_reconciliation_timer.sh" infra/profitability/

systemctl() {
    case "$1" in
        is-active) test -e timer_active || return 3 ;;
        show) printf '%s\n' "$timer_load_state" ;;
        start) touch timer_active ;;
        *) return 2 ;;
    esac
}
systemd-run() {
    printf '%s\n' "$@" > timer_arguments
    touch timer_active
}
docker() {
    [[ "$*" == 'exec ai-football-v2-profitability-watch printenv PROFITABILITY_ANCHOR' ]] || return 2
    printf '%s\n' "$restore_anchor"
}
export -f systemctl systemd-run docker

restore() {
    bash infra/profitability/restore_reconciliation_timer.sh
}

export timer_load_state=not-found
export restore_anchor=invalid
result=0
restore || result=$?
test "$result" = 2
test ! -e timer_arguments
test ! -e timer_active

export restore_anchor="$anchor"
restore
state_path="$test_dir/.runtime/profitability-watch/reconciliations"
printf '%s\n' '--unit=ai-football-profitability-reconcile-rolling' '--collect' '--on-active=15s' \
    '--on-calendar=*-*-* 02,14:25,45:00 UTC' '--timer-property=AccuracySec=1s' \
    '--property=Type=oneshot' '--property=TimeoutStartSec=75s' /usr/bin/bash \
    "$test_dir/infra/profitability/reconcile_due_windows.sh" "$anchor" "$state_path" > expected_arguments
cmp timer_arguments expected_arguments

rm timer_arguments
export restore_anchor=invalid
restore
test ! -e timer_arguments

rm timer_active
export restore_anchor="$anchor" timer_load_state=loaded
restore
test -e timer_active
test ! -e timer_arguments
printf '%s\n' 'Reconciliation timer recovery checks passed.'
