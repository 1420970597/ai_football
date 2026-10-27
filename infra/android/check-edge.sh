#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
task_network=ai-football-edge-$$
task_api=ai-football-edge-api-$$
task_edge=ai-football-edge-nginx-$$
task_python=python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
task_nginx=nginx:1.29-alpine@sha256:5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de
cleanup() {
  task_exit=$?
  docker rm -f "$task_edge" "$task_api" >/dev/null 2>&1 || true
  docker network rm "$task_network" >/dev/null 2>&1 || true
  exit "$task_exit"
}
trap cleanup EXIT INT TERM
docker network create --internal --label ai-football.task=edge "$task_network" >/dev/null
docker run -d --name "$task_api" --network "$task_network" --network-alias api --cpus=.25 --memory=128m --pids-limit=64 \
  --read-only --cap-drop ALL --security-opt no-new-privileges -v "$task_root/tests/android:/probe:ro" \
  "$task_python" python /probe/edge_probe.py serve >/dev/null
docker run -d --name "$task_edge" --network "$task_network" --network-alias edge --cpus=.25 --memory=96m --pids-limit=64 \
  --read-only --tmpfs /var/cache/nginx:size=8m --tmpfs /var/run:size=1m --tmpfs /tmp:size=2m \
  -v "$task_root/web/app/nginx.conf:/etc/nginx/conf.d/default.conf:ro" "$task_nginx" >/dev/null
docker run --rm --network "$task_network" --cpus=.25 --memory=128m --pids-limit=64 --read-only --cap-drop ALL \
  --security-opt no-new-privileges -v "$task_root/tests/android:/probe:ro" "$task_python" python /probe/edge_probe.py
