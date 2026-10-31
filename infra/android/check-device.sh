#!/bin/sh
# A device-ready result only permits UI execution; it is not an acceptance result.
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
task_network=${ANDROID_DEVICE_NETWORK:-none}
task_host=${ANDROID_ADB_HOST:-127.0.0.1}
task_serial=${ANDROID_DEVICE_SERIAL:-}
case "$task_network" in none|ai-football-*) ;; *) echo 'Use only a project-owned Android network.' >&2; exit 2;; esac
mkdir -p "$task_root/.runtime/android-device-evidence"
docker build --memory 384m --cpu-period 100000 --cpu-quota 25000 --target toolchain \
  -f "$task_root/android/Dockerfile" -t ai-football-android-toolchain:v2.0.0-dev "$task_root" >/dev/null
docker build --memory 256m --cpu-period 100000 --cpu-quota 25000 \
  -f "$task_root/infra/android/device-tools.Dockerfile" -t ai-football-android-device-tools:v2.0.0-dev "$task_root" >/dev/null
set -- --host "$task_host"
if [ -n "$task_serial" ]; then set -- "$@" --serial "$task_serial"; fi
docker run --rm --cpus=.25 --memory=256m --pids-limit=128 --network "$task_network" \
  -v "$task_root:/source:ro" -v "$task_root/.runtime/android-device-evidence:/evidence" \
  --entrypoint python3 ai-football-android-device-tools:v2.0.0-dev \
  /source/tests/android/device_preflight.py "$@" \
  --output /evidence/preflight.json
