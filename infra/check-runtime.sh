#!/bin/sh
# 仅做项目内假配置解析，不启动服务，不读取项目外配置。
set -eu
task_project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$task_project_root"
docker run --rm --user "$(id -u):$(id -g)" --cpus=0.5 --memory=192m --network=none \
  -e PYTHONDONTWRITEBYTECODE=1 -v "$task_project_root:/w" -w /w \
  python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 python infra/config/make_test_environment.py
docker compose --env-file .runtime/compose-test.env config --format json > .runtime/compose-test.json
docker run --rm --user "$(id -u):$(id -g)" --cpus=0.5 --memory=192m --network=none \
  -e PYTHONDONTWRITEBYTECODE=1 -v "$task_project_root:$task_project_root:ro" -w "$task_project_root" \
  python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 python infra/config/check_compose.py .runtime/compose-test.json --fixture-env .runtime/compose-test.env
docker compose --project-name ai-football-config-probe --env-file .runtime/compose-test.env \
  -f infra/config/compose-env-test.yml run --rm --no-deps value-probe
