#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# != 1 ]]; then
    printf '%s\n' 'Usage: collect_analysis.sh <private monitor root>' >&2
    exit 2
fi
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
monitor=$(cd -- "$1" && pwd)
archive="$monitor/analysis"
install -d -m 700 -- "$archive"
exec 9>"$archive/.collect.lock"
flock -n 9 || exit 0
stamp=$(date -u +%Y%m%dT%H%M%S.%6NZ)
work=$(mktemp -d -- "$archive/.collect.XXXXXX")
trap 'rm -f -- "$work/input.json" "$work/output/report.json" "$work/output/report.md" "$work/latest"; rmdir -- "$work/output" "$work" 2>/dev/null || true' EXIT
mkdir -- "$work/output"

sql=("$root/"*.sql)
tar -C "$root" -cf - -- "${sql[@]##*/}" |
    docker exec -i ai-football-v2-api-1 python -c "$(< "$root/collect_analysis.py")" > "$work/input.json"
docker run --rm --user "$(id -u):$(id -g)" --network none --cpus .25 --memory 128m \
    --entrypoint python -w /research -v "$root:/research/infra/profitability:ro" \
    -v "$monitor:/monitor:ro" -v "$work:/work" ai-football-verifier:v2.0.0-dev \
    -B -m infra.profitability.analysis_report /work/input.json /monitor /work/output
test -s "$work/output/report.json"
test -s "$work/output/report.md"
mv -T -- "$work/output" "$archive/$stamp"
ln -s -- "$stamp" "$work/latest"
mv -Tf -- "$work/latest" "$archive/latest"
printf 'Published profitability analysis for %s\n' "$stamp"
