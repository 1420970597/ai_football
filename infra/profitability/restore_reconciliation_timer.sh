#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
state_dir=$(cd -- "$root/../../.runtime/profitability-watch/reconciliations" && pwd)
unit=ai-football-profitability-reconcile-rolling

if systemctl is-active --quiet "$unit.timer"; then
    printf '%s\n' 'Rolling reconciliation timer is active.'
    exit 0
fi

anchor=$(docker exec ai-football-v2-profitability-watch printenv PROFITABILITY_ANCHOR)
if [[ "$anchor" != *Z && "$anchor" != *+00:00 ]] || ! date -u -d "$anchor" +%s >/dev/null; then
    printf '%s\n' 'The running monitor must supply its fixed UTC anchor.' >&2
    exit 2
fi

if [[ $(systemctl show "$unit.timer" -p LoadState --value) == loaded ]]; then
    systemctl start "$unit.timer"
else
    systemd-run --unit="$unit" --collect --on-active=15s \
        --on-calendar='*-*-* 02,14:25,45:00 UTC' --timer-property=AccuracySec=1s \
        --property=Type=oneshot --property=TimeoutStartSec=75s \
        /usr/bin/bash "$root/reconcile_due_windows.sh" "$anchor" "$state_dir"
fi
systemctl is-active --quiet "$unit.timer"
