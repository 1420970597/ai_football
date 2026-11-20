#!/bin/sh
# Exercise the shipped runtime account against a private frozen build context.
set -eu
cd "$(dirname "$0")/.."
task_suffix=$$
task_root="$PWD/.runtime/api-image-smoke-$task_suffix"
task_image="ai-football-api:permission-smoke-$task_suffix"
umask 077
mkdir -p "$task_root/context/services/api" "$task_root/evidence"
cp .dockerignore "$task_root/context/.dockerignore"
cp -R services/api/app services/api/migrations services/api/requirements.txt services/api/Dockerfile "$task_root/context/services/api/"
cp services/quote_witness.py services/execution_scope.py services/basketball_scores.py "$task_root/context/services/"
# Git does not record read bits. Reproduce the private worktree that caused
# a byte-for-byte valid image to be unreadable by its unprivileged runtime.
find "$task_root/context/services/api/app" "$task_root/context/services/api/migrations" -type d -exec chmod 700 {} +
find "$task_root/context/services/api/app" "$task_root/context/services/api/migrations" -type f -exec chmod 600 {} +
docker build --load --memory 512m --cpu-period 100000 --cpu-quota 50000 \
  -f "$task_root/context/services/api/Dockerfile" -t "$task_image" "$task_root/context"
docker run --rm -i --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --cpus .25 --memory 256m \
  --entrypoint python -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/app \
  -e EXPECTED_QUOTE_WITNESS_SHA256="$(sha256sum services/quote_witness.py | cut -d ' ' -f1)" \
  -e EXPECTED_EXECUTION_SCOPE_SHA256="$(sha256sum services/execution_scope.py | cut -d ' ' -f1)" \
  -e EXPECTED_BASKETBALL_SCORES_SHA256="$(sha256sum services/basketball_scores.py | cut -d ' ' -f1)" \
  "$task_image" - < infra/test/api_image_smoke.py > "$task_root/evidence/api-image-smoke.json"
printf 'API image runtime evidence: %s\n' "$task_root/evidence/api-image-smoke.json"
