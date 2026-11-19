#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
project_root=$(cd -- "$root/../.." && pwd)
archive="$project_root/.runtime/profitability-watch/public-orderbook-pilot-20261118"
bindings=fixture-bindings-20261119.json
interval=33
last_offset=$((237 * interval))

if [[ ${1-} == --capture ]]; then
    shift
    begin=$(date -u -d "$1" +%s)
    now=$(date -u +%s)
    remaining=$((begin + last_offset - now))
    if (( now < begin || remaining < 0 )); then
        printf '%s\n' 'The scheduled sampling window is closed or has not started.'
        exit 0
    fi
    shift
    exec bash "$root/capture_reference_window.sh" "$1" \
        --samples "$((remaining / interval + 1))" --interval-seconds "$interval" "${@:2}"
fi

if (( $# != 0 )); then
    printf '%s\n' 'Usage: restore_reference_pilot.sh' >&2
    exit 2
fi
if [[ ! -s "$archive/$bindings" || ! -r "$archive/$bindings" ]]; then
    printf '%s\n' 'The original fixture bindings are unavailable.' >&2
    exit 2
fi

unit_active() {
    systemctl is-active --quiet "$1.timer" || systemctl is-active --quiet "$1.service"
}

register_once() {
    local unit=$1 epoch=$2 timeout=$3
    shift 3
    local calendar
    calendar=$(date -u -d "@$epoch" '+%F %T UTC')
    systemd-run --unit="$unit" --collect --on-calendar="$calendar" \
        --timer-property=AccuracySec=1s --property=Type=oneshot \
        --property="TimeoutStartSec=$timeout" /usr/bin/bash "$@"
}

has_archive() {
    local path
    for path in "$@"; do
        if [[ -s "$path" ]]; then
            return 0
        fi
    done
    return 1
}

restore_fixture() {
    local name=$1 label=$2 fixture=$3 begin=$4 end=$5 home=$6 away=$7 handicap=$8
    shift 8
    local capture_unit="ai-football-profitability-reference-$name"
    local replay_unit="ai-football-profitability-reference-replay-$name"
    local now begin_epoch end_epoch earliest capture_at replay_at
    now=$(date -u +%s)
    begin_epoch=$(date -u -d "$begin" +%s)
    end_epoch=$(date -u -d "$end" +%s)
    earliest=$((now + 5))
    capture_at=$((begin_epoch > earliest ? begin_epoch : earliest))
    replay_at=$((end_epoch > earliest ? end_epoch : earliest))

    if unit_active "$capture_unit"; then
        printf '%s\n' "$name capture is active."
    elif (( capture_at <= begin_epoch + last_offset )); then
        register_once "$capture_unit" "$capture_at" 9000s \
            "$root/restore_reference_pilot.sh" --capture "$begin" "$label" \
            --polymarket "$label-$home" --polymarket "$label-draw" --polymarket "$label-$away" \
            --polymarket "$label-total-2pt5" --polymarket "$label-first-half-total-1pt5" \
            --polymarket "$label-spread-$handicap-1pt5" "$@"
    else
        printf '%s\n' "$name capture window has elapsed."
    fi

    if has_archive "$archive/$label-replay-"*.json; then
        printf '%s\n' "$name replay is already archived."
    elif unit_active "$replay_unit"; then
        printf '%s\n' "$name replay is active."
    elif (( now < end_epoch )) || has_archive "$archive/$label-"*.jsonl; then
        register_once "$replay_unit" "$replay_at" 90s \
            "$root/replay_reference_window.sh" "$label" "$fixture" "$begin" "$end" "$bindings"
    else
        printf '%s\n' "$name has no captured observations to replay."
    fi
}

restore_fixture svay aseanc-svr-por-2026-11-19 leyu:5860873 \
    2026-11-19T11:57:00Z 2026-11-19T14:32:00Z svr por home
restore_fixture vienna uwcl-faw-bm-2026-11-19 leyu:5831446 \
    2026-11-19T17:42:00Z 2026-11-19T20:17:00Z faw bm away \
    --kalshi KXUCLWTOTAL-26NOV19FAKBMU-3 --kalshi KXUCLWSPREAD-26NOV19FAKBMU-BMU2
restore_fixture vasco bra-vas-int-2026-11-19 leyu:5816659 \
    2026-11-19T22:27:00Z 2026-11-20T01:02:00Z vas int home \
    --kalshi KXBRASILEIROTOTAL-26NOV19VDGINT-3
