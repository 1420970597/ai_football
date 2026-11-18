#!/bin/sh
set -eu
umask 077

: "${PROFITABILITY_ANCHOR:?fixed UTC anchor required}"
export PGCONNECT_TIMEOUT=5
export PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=8000 -c lock_timeout=1000 -c TimeZone=UTC'

trap 'exit 0' INT TERM
mkdir -p /state/checkpoints /state/window-closes /state/wallet-observations
archive_from=0
while :; do
    next_poll=300
    while test -s "/state/window-closes/$archive_from.json"; do
        archive_from=$((archive_from + 1))
    done
    if psql -X -qAt -v ON_ERROR_STOP=1 -v "anchor=$PROFITABILITY_ANCHOR" -v watch_mode=1 \
        -v "archive_from=$archive_from" -v align_window_poll=1 \
        -f /watch/snapshot.sql > /state/.watch.tmp 2>/dev/null \
        && { IFS= read -r window_delay && IFS= read -r closed_ordinals; } < /state/.watch.tmp \
        && test "$window_delay" -ge 1 && test "$window_delay" -le 300 \
        && tail -n +3 /state/.watch.tmp > /state/.latest.tmp && test -s /state/.latest.tmp; then
        next_poll=$window_delay
        snapshot_at=$(date -u +%Y%m%dT%H%M%SZ)
        mv /state/.latest.tmp /state/latest.json
        checkpoint="/state/checkpoints/$snapshot_at.json"
        test -e "$checkpoint" || ln /state/latest.json "$checkpoint"
        for ordinal in $closed_ordinals; do
            closed="/state/window-closes/$ordinal.json"
            test -e "$closed" || ln /state/latest.json "$closed"
        done
        if test -s /state/window-closes/3.json && ! test -e /state/window-closes/initial-48h.json; then
            ln /state/window-closes/3.json /state/window-closes/initial-48h.json
        fi
        find /state/checkpoints -type f -name '*.json' -mmin +4320 -delete
        date -u +%FT%TZ > /state/.last_success.tmp
        mv /state/.last_success.tmp /state/last_success
        rm -f /state/last_failure
        if psql -X -qAt -v ON_ERROR_STOP=1 -f /watch/wallet_observation.sql > /state/.wallet.tmp 2>/dev/null && test -s /state/.wallet.tmp; then
            wallet="/state/wallet-observations/$snapshot_at.json"
            test -e "$wallet" || mv /state/.wallet.tmp "$wallet"
            rm -f /state/.wallet.tmp
            wallet_latest="/state/.wallet-latest.tmp"
            cp "$wallet" "$wallet_latest"
            mv "$wallet_latest" /state/wallet-latest.json
            rm -f /state/last_wallet_failure
        else
            rm -f /state/.wallet.tmp
            date -u +%FT%TZ > /state/.last_wallet_failure.tmp
            mv /state/.last_wallet_failure.tmp /state/last_wallet_failure
            printf '%s\n' 'profitability wallet observation failed' >&2
        fi
        find /state/wallet-observations -type f -name '*.json' -mmin +4320 -delete
    else
        date -u +%FT%TZ > /state/.last_failure.tmp
        mv /state/.last_failure.tmp /state/last_failure
        rm -f /state/.latest.tmp /state/.wallet.tmp /state/.last_success.tmp
        printf '%s\n' 'profitability snapshot failed' >&2
    fi
    rm -f /state/.watch.tmp
    sleep "$next_poll" &
    wait "$!"
done
