#!/bin/sh
# Reuse the browser image built by the preceding responsive verification stage.
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
task_evidence="$task_root/.runtime/model-configuration-ui-$$"
task_snapshot="$task_evidence/source"
mkdir -p "$task_snapshot/web" "$task_snapshot/tests/browser"
cp -a "$task_root/web/app" "$task_snapshot/web/"
cp "$task_root/tests/browser/model-configuration.mjs" "$task_snapshot/tests/browser/"
docker image inspect ai-football-browser:v2.0.0-dev >/dev/null
docker run --rm --cpus=.5 --memory=512m --shm-size=128m --pids-limit=256 --network none \
  -e MODEL_UI_RESULTS=/evidence/results.json \
  -v "$task_snapshot:/work:ro" -v "$task_evidence:/evidence" -w /work \
  ai-football-browser:v2.0.0-dev tests/browser/model-configuration.mjs
printf 'model_configuration_ui_evidence=%s\n' "$task_evidence"
