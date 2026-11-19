#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# != 1 ]]; then
    printf '%s\n' 'Usage: collect_window_evidence.sh <private monitor root>' >&2
    exit 2
fi
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
monitor=$(cd -- "$1" && pwd)
archive="$monitor/window-evidence"
install -d -m 700 -- "$archive"
draft=$(mktemp -- "$archive/.evidence.XXXXXX")
trap 'rm -f -- "$draft"' EXIT
docker run --rm --user "$(id -u):$(id -g)" --network none --cpus .25 --memory 128m \
    --entrypoint python -v "$root:/research:ro" -v "$monitor:/evidence:ro" \
    ai-football-verifier:v2.0.0-dev /research/window_evidence.py /evidence --max-snapshot-age-seconds 660 > "$draft"
target="$archive/$(date -u +%FT%T.%6NZ).json"
ln -T -- "$draft" "$target"
printf 'Archived settlement verification in %s\n' "$(basename -- "$target")"
