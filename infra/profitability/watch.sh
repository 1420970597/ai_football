#!/bin/sh
set -eu
umask 077

: "${PROFITABILITY_ANCHOR:?fixed UTC anchor required}"
export PGCONNECT_TIMEOUT=5
export PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=8000 -c lock_timeout=1000 -c TimeZone=UTC'

trap 'exit 0' INT TERM
while :; do
    if psql -X -qAt -v ON_ERROR_STOP=1 -v "anchor=$PROFITABILITY_ANCHOR" -f /watch/snapshot.sql \
        > /state/.latest.tmp 2>/dev/null && test -s /state/.latest.tmp; then
        mv /state/.latest.tmp /state/latest.json
        snapshot_at=$(date -u +%Y%m%dT%H%M%SZ)
        mkdir -p /state/checkpoints
        checkpoint="/state/checkpoints/$snapshot_at.json"
        test -e "$checkpoint" || ln /state/latest.json "$checkpoint"
        find /state/checkpoints -type f -name '*.json' -mmin +4320 -delete
        date -u +%FT%TZ > /state/.last_success.tmp
        mv /state/.last_success.tmp /state/last_success
        rm -f /state/last_failure
    else
        date -u +%FT%TZ > /state/.last_failure.tmp
        mv /state/.last_failure.tmp /state/last_failure
        rm -f /state/.latest.tmp /state/.last_success.tmp
        printf '%s\n' 'profitability snapshot failed' >&2
    fi
    sleep 300 &
    wait "$!"
done
