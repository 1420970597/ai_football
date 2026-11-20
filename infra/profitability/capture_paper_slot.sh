#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# -lt 2 || $# -gt 3 || ( $# = 3 && "$3" != "--projection-only" ) ]]; then
    printf '%s\n' 'Usage: capture_paper_slot.sh <private archive> <UTC minute or now> [--projection-only]' >&2
    exit 2
fi

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
archive=$1
slot=$2
if [[ "$slot" == now ]]; then
    slot=$(date -u +%Y-%m-%dT%H:%M:00Z)
fi
if [[ ! "$slot" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:00Z$ ]]; then
    exit 2
fi
shift 2
install -d -m 700 -- "$archive"
exec 9>"$archive/.lock"
flock -n 9 || exit 75
target="$archive/$slot.json"
if [[ -e "$target" ]]; then
    printf 'Paper slot already archived %s\n' "$slot"
    exit 0
fi
draft=$(mktemp -- "$archive/.paper.XXXXXX")
trap 'rm -f -- "$draft"' EXIT
checker=$(base64 -w0 "$root/paper_capture.py")
pricing=$(base64 -w0 "$root/paper_price.py")
docker exec -i ai-football-v2-api-1 python - "$slot" "$@" < "$root/paper_capture_candidates.py" |
    docker exec -i ai-football-v2-executor-1 python -c \
        'import base64, sys; exec(compile(base64.b64decode(sys.argv[1]), "paper_capture.py", "exec"))' "$checker" |
    docker exec -i ai-football-v2-api-1 python -c \
        'import base64, sys; exec(compile(base64.b64decode(sys.argv[1]), "paper_price.py", "exec"))' "$pricing" > "$draft"
test -s "$draft"
ln -T -- "$draft" "$target"
printf 'Archived paper slot %s\n' "$slot"
