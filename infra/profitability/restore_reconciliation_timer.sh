#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
project_root=$(cd -- "$root/../.." && pwd)
state_dir=$(cd -- "$project_root/.runtime/profitability-watch/reconciliations" && pwd)
unit=ai-football-profitability-reconcile-rolling

if [[ $# -gt 1 || ( $# == 1 && "$1" != --with-pilot ) ]]; then
    printf '%s\n' 'Usage: restore_reconciliation_timer.sh [--with-pilot]' >&2
    exit 2
fi

restore_timer() {
    local name=$1 calendar=$2 accuracy=$3 timeout=$4
    shift 4
    if systemctl is-active --quiet "$name.timer"; then
        return
    fi
    if [[ $(systemctl show "$name.timer" -p LoadState --value) == loaded ]]; then
        systemctl start "$name.timer"
    else
        systemd-run --unit="$name" --collect --on-active=15s \
            --on-calendar="$calendar" --timer-property="AccuracySec=$accuracy" \
            --property=Type=oneshot --property="TimeoutStartSec=$timeout" "$@"
    fi
    systemctl is-active --quiet "$name.timer"
}

if ! systemctl is-active --quiet "$unit.timer"; then
    anchor=$(docker exec ai-football-v2-profitability-watch printenv PROFITABILITY_ANCHOR)
    if [[ "$anchor" != *Z && "$anchor" != *+00:00 ]] || ! date -u -d "$anchor" +%s >/dev/null; then
        printf '%s\n' 'The running monitor must supply its fixed UTC anchor.' >&2
        exit 2
    fi
    restore_timer "$unit" '*-*-* 02,14:25,45:00 UTC' 1s 75s \
        /usr/bin/bash "$root/reconcile_due_windows.sh" "$anchor" "$state_dir"
fi
printf '%s\n' 'Rolling reconciliation timer is active.'

if [[ $# == 1 ]]; then
    preflight_dir="$project_root/.runtime/profitability-watch/preflight-cohorts-20261118T1600Z"
    if [[ ! -s "$preflight_dir/final.json" ]]; then
        restore_timer ai-football-profitability-preflight-cohorts '*-*-* *:05:00 UTC' 1s 75s \
            /usr/bin/bash "$root/collect_preflight_cohorts.sh" 2026-11-18T16:00:00Z "$preflight_dir"
    fi
    if [[ $(date -u +%Y-%m-%dT%H:%M:%SZ) < 2026-11-20T02:15:00Z ]]; then
        source_script="$project_root/.runtime/profitability-telegram-preview-20261118/collect-active.sh"
        if [[ ! -r "$source_script" ]]; then
            printf '%s\n' 'The original public-preview pilot configuration is unavailable.' >&2
            exit 2
        fi
        restore_timer ai-football-profitability-telegram-preview '*-*-* *:00,30:00 UTC' 5s 80s \
            /usr/bin/bash "$source_script"
    fi
fi
