#!/bin/sh
# Render only synthetic configuration; never start workers or read live secrets.
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$task_root"
task_output=.runtime/remote-deployment-20261027
mkdir -p "$task_output"
docker run --rm --network none --cpus .25 --memory 128m --pids-limit 32 \
  --user "$(id -u):$(id -g)" -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$task_root:/w" -w /w \
  python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 \
  python infra/config/make_test_environment.py
export REMOTE_COLLECTOR_IMAGE=registry.invalid/platform-collector@sha256:2222222222222222222222222222222222222222222222222222222222222222
export REMOTE_EXECUTOR_IMAGE=registry.invalid/platform-executor@sha256:2222222222222222222222222222222222222222222222222222222222222222
export REMOTE_TUNNEL_IMAGE=registry.invalid/platform-tunnel@sha256:2222222222222222222222222222222222222222222222222222222222222222
docker compose --project-directory "$task_root" --env-file .runtime/compose-test.env \
  -f infra/remote/compose.remote.yml config --format json > "$task_output/remote-compose.json"
docker compose --project-directory "$task_root" --env-file .runtime/compose-test.env \
  --profile legacy-local-workers -f docker-compose.yml -f infra/remote/compose.local.yml config --format json > "$task_output/local-compose.json"
docker compose --project-directory "$task_root" --env-file .runtime/compose-test.env \
  -f docker-compose.yml -f infra/remote/compose.local.yml config --services > "$task_output/default-services.txt"
if grep -Eq '^(collector|executor)$' "$task_output/default-services.txt"; then
  echo local_default_workers_unexpected
  exit 1
fi
docker run --rm --network none --cpus .25 --memory 128m --pids-limit 32 \
  -e PYTHONDONTWRITEBYTECODE=1 -v "$task_root:/w:ro" -w /w \
  python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 \
  python -m infra.remote.validate_compose "$task_output/local-compose.json" "$task_output/remote-compose.json"
