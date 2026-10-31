#!/bin/sh
# One task-owned, offline container exercises the rendered tmpfs at Docker create.
set -eu
task_name="ai-football-remote-tmpfs-contract-$$"
if docker inspect "$task_name" >/dev/null 2>&1; then
  printf '%s\n' 'task_container_name_collision' >&2
  exit 1
fi
trap 'docker rm -f "$task_name" >/dev/null 2>&1 || true' EXIT HUP INT TERM
docker create --name "$task_name" --network none --read-only --cap-drop ALL \
  --cpus .1 --memory 48m --pids-limit 16 --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
  --entrypoint python python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 \
  -c 'import pathlib;pathlib.Path("/tmp/contract").write_text("synthetic");print("tmpfs_create_and_write_verified")' >/dev/null
docker start -a "$task_name"
test "$(docker inspect "$task_name" --format '{{.State.ExitCode}}')" = 0
docker rm "$task_name" >/dev/null
trap - EXIT HUP INT TERM
