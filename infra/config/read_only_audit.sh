#!/usr/bin/env bash
set -euo pipefail

# Run the archive/config audit in an isolated, network-disabled container.
# The archive is mounted read-only and the only writable mount is this repo.

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "usage: $0 ARCHIVE [PROJECT_RELATIVE_OUTPUT]" >&2
  exit 2
fi

repo_root="$(cd -- "$(dirname -- "$0")/../.." && pwd -P)"
archive="$(realpath -e -- "$1")"
if [[ ! -f "$archive" ]]; then
  echo "archive input is not a regular file" >&2
  exit 2
fi

output_arg="${2:-.runtime/config-audit/read-only-config-audit.json}"
if [[ "$output_arg" = /* ]]; then
  output="$(realpath -m -- "$output_arg")"
else
  output="$(realpath -m -- "$repo_root/$output_arg")"
fi
case "$output" in
  "$repo_root"/*) ;;
  *)
    echo "output must be inside project root" >&2
    exit 2
    ;;
esac

docker run --rm \
  --network none \
  --cpus .5 \
  --memory 256m \
  --user "$(id -u):$(id -g)" \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$repo_root:/work" \
  -v "$archive:/input/archive.tar:ro" \
  -w /work \
  python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 \
  python -m infra.config.read_only_audit \
    --archive /input/archive.tar \
    --project-root /work \
    --output "/work/${output#"$repo_root/"}"
