#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
project_root=$(cd -- "$root/../.." && pwd)
archive="$project_root/.runtime/profitability-watch/public-orderbook-pilot-20261118"
bindings=fixture-bindings-20261119.json
interval=33
last_offset=$((237 * interval))

if [[ ${1-} == --capture || ${1-} == --score ]]; then
    mode=$1
    shift
    begin=$(date -u -d "$1" +%s)
    now=$(date -u +%s)
    remaining=$((begin + last_offset - now))
    if (( now < begin || remaining < 0 )); then
        printf '%s\n' 'The scheduled sampling window is closed or has not started.'
        exit 0
    fi
    shift
    capture_args=()
    if [[ "$mode" == --score ]]; then
        capture_args=(--score)
    fi
    exec bash "$root/capture_reference_window.sh" "${capture_args[@]}" "$1" \
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
    systemctl is-active --quiet "$1.timer" ||
        [[ $(systemctl show "$1.service" --property=ActiveState --value) =~ ^(active|activating)$ ]]
}

restore_score() {
    local unit="ai-football-profitability-reference-score-$1" begin=$2 label=$3
    shift 3
    local begin_epoch earliest capture_at
    begin_epoch=$(date -u -d "$begin" +%s)
    earliest=$(($(date -u +%s) + 5))
    capture_at=$((begin_epoch > earliest ? begin_epoch : earliest))
    if unit_active "$unit"; then
        printf '%s\n' "$label score observation is active."
    elif (( capture_at <= begin_epoch + last_offset )); then
        register_once "$unit" "$capture_at" 9000s \
            "$root/restore_reference_pilot.sh" --score "$begin" "$label" "$@"
    fi
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
    local stream_unit="ai-football-profitability-reference-stream-$name"
    local replay_unit="ai-football-profitability-reference-replay-$name"
    local now begin_epoch end_epoch earliest capture_at stream_at stream_start stream_end replay_at
    now=$(date -u +%s)
    begin_epoch=$(date -u -d "$begin" +%s)
    end_epoch=$(date -u -d "$end" +%s)
    earliest=$((now + 5))
    capture_at=$((begin_epoch > earliest ? begin_epoch : earliest))
    stream_at=$((begin_epoch + 180))
    stream_start=$((stream_at > earliest ? stream_at : earliest))
    stream_end=$((stream_at + 900))
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

    if unit_active "$stream_unit"; then
        printf '%s\n' "$name public stream observation is active."
    elif (( stream_start + 5 <= stream_end )); then
        register_once "$stream_unit" "$stream_start" 930s \
            "$root/capture_reference_window.sh" --stream "$label" --seconds 900 --end-at "$stream_end" \
            "$label-$home" "$label-draw" "$label-$away" "$label-total-2pt5" \
            "$label-first-half-total-1pt5" "$label-spread-$handicap-1pt5"
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
restore_fixture benfica uwcl-slb-asr-2026-11-19 leyu:5831469 \
    2026-11-19T19:57:00Z 2026-11-19T22:32:00Z slb asr home
restore_fixture vasco bra-vas-int-2026-11-19 leyu:5816659 \
    2026-11-19T22:27:00Z 2026-11-20T01:02:00Z vas int home \
    --kalshi KXBRASILEIROTOTAL-26NOV19VDGINT-3

restore_score vienna 2026-11-19T17:42:00Z uwcl-faw-bm-2026-11-19 \
    401916978 VIE MUN --soccer-league uefa.wchampions --kickoff 2026-11-19T17:45:00Z
restore_score benfica 2026-11-19T19:57:00Z uwcl-slb-asr-2026-11-19 \
    401916974 BEN ROMA --soccer-league uefa.wchampions --kickoff 2026-11-19T20:00:00Z
restore_score vasco 2026-11-19T22:27:00Z bra-vas-int-2026-11-19 \
    401841308 VAS INT --soccer-league bra.1 --kickoff 2026-11-19T22:30:00Z

adjacent_unit=ai-football-profitability-reference-vasco-adjacent
adjacent_begin=2026-11-19T22:27:00Z
adjacent_epoch=$(date -u -d "$adjacent_begin" +%s)
earliest=$(($(date -u +%s) + 5))
adjacent_at=$((adjacent_epoch > earliest ? adjacent_epoch : earliest))
if unit_active "$adjacent_unit"; then
    printf '%s\n' 'Vasco adjacent totals observation is active.'
elif (( adjacent_at <= adjacent_epoch + last_offset )); then
    register_once "$adjacent_unit" "$adjacent_at" 9000s \
        "$root/restore_reference_pilot.sh" --capture "$adjacent_begin" vasco-adjacent-totals \
        --polymarket bra-vas-int-2026-11-19-total-2pt5 --polymarket bra-vas-int-2026-11-19-total-3pt5 \
        --kalshi KXBRASILEIROTOTAL-26NOV19VDGINT-3 --kalshi KXBRASILEIROTOTAL-26NOV19VDGINT-4
fi
