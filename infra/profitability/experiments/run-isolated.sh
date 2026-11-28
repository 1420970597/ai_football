#!/usr/bin/env bash
set -euo pipefail

if (( $# < 5 )) || [[ $4 != -- ]]; then
    printf '%s\n' 'Usage: bash run-isolated.sh <source-commit> <frozen-evidence-directory> <public-data-directory> -- <python arguments...>' >&2
    exit 2
fi

revision=$1
input=$(realpath -e -- "$2")
public=$(realpath -e -- "$3")
shift 4
[[ -d $input && -d $public ]]
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd -P)
commit=$(git -C "$root" rev-parse --verify --end-of-options "$revision^{commit}")
umask 077
runtime="$root/.runtime/profitability-experiments-20261122"
mkdir -p -- "$runtime"
source=$(mktemp -d -- "$runtime/.isolated-source.XXXXXX")
trap 'rm -rf -- "$source"' EXIT
git -C "$root" archive "$commit" infra/profitability services \
    reports/profitability-pilot-20261118 tests | tar -xf - -C "$source"

# This is the existing locally verified research runtime. A different image must
# be evaluated independently and recorded with the resulting experiment.
image=sha256:b3910d921ed6fcb2968129aa43bac0803895de46ace2881351ea3e623ee6d93c
docker run --rm -i --pull never --network none --read-only \
    --cpus .5 --memory 384m --pids-limit 64 \
    --cap-drop ALL --security-opt no-new-privileges \
    --user "$(id -u):$(id -g)" --tmpfs /tmp:rw,exec,nosuid,nodev,size=64m \
    --mount "type=bind,src=$source,dst=/src,readonly" \
    --mount "type=bind,src=$input,dst=/input,readonly" \
    --mount "type=bind,src=$public,dst=/public,readonly" \
    --workdir /src -e PYTHONDONTWRITEBYTECODE=1 -e PYTEST_ADDOPTS='-p no:cacheprovider' \
    -e PYTHONPATH=/src:/src/services/api:/src/services:/src/reports/profitability-pilot-20261118 \
    "$image" python "$@"
