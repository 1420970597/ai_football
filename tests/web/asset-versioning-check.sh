#!/bin/sh
# Actual Dockerfile + nginx, two deployment generations, no production services.
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
task_id="web-assets-$$"
task_output="$task_root/.runtime/$task_id"
task_network="ai-football-$task_id"
task_base=$(sed -n 's/^FROM //p' "$task_root/web/app/Dockerfile" | head -n 1)
task_image="ai-football-web:$task_id"
cleanup() {
    task_result=$?
    trap - EXIT INT TERM
    for service in web next api; do
        docker logs "$task_id-$service" > "$task_output/$service.log" 2>&1 || true
        docker rm -f "$task_id-$service" >/dev/null 2>&1 || true
    done
    docker network rm "$task_network" >/dev/null 2>&1 || true
    printf 'asset_acceptance_exit=%s evidence=%s\n' "$task_result" "$task_output"
    exit "$task_result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p "$task_output"
docker run --rm --network none --cpus .25 --memory 128m --entrypoint sh \
    -v "$task_root:/source:ro" -v "$task_output:/evidence" \
    "$task_base" /source/tests/web/asset-versioning-build.sh
docker build --memory 768m --cpu-period 100000 --cpu-quota 50000 \
    -f "$task_root/web/app/Dockerfile" -t "$task_image" "$task_root"
docker network create --internal --subnet "${ASSET_TEST_SUBNET:-10.93.79.16/28}" \
    --label ai-football.task=asset-versioning "$task_network" >/dev/null
cat > "$task_output/api.conf" <<'CONF'
server { listen 8080; location / { default_type application/json; return 200 '{"status":"isolated"}'; } }
CONF
chmod -R a+rX "$task_output"
docker run -d --name "$task_id-api" --network "$task_network" --network-alias api \
    --cpus .25 --memory 64m -v "$task_output/api.conf:/etc/nginx/conf.d/default.conf:ro" "$task_base" >/dev/null
docker run -d --name "$task_id-web" --network "$task_network" --network-alias web \
    --cpus .25 --memory 64m "$task_image" >/dev/null
docker run -d --name "$task_id-next" --network "$task_network" --network-alias next \
    --cpus .25 --memory 64m -v "$task_root/web/app/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
    -v "$task_output/builds/css:/usr/share/nginx/html:ro" "$task_base" >/dev/null
task_ready=0
until docker exec "$task_id-web" curl -fsS http://next/healthz >/dev/null 2>&1; do
    task_ready=$((task_ready + 1))
    test "$task_ready" -lt 30
    sleep 1
done
docker run --rm --network "$task_network" --cpus .5 --memory 768m --pids-limit 256 \
    -e ASSET_BASE_A=http://web -e ASSET_BASE_B=http://next -e BROWSER_EVIDENCE_DIR=/evidence \
    -v "$task_root:/work:ro" -v "$task_output:/evidence" ai-football-browser:v2.0.0-dev tests/browser/asset-versioning.mjs
docker run --rm --network "$task_network" --cpus .5 --memory 768m --pids-limit 256 \
    -e VISUAL_BASE_URL=http://web -e VISUAL_RESULTS=/evidence \
    -v "$task_root:/work:ro" -v "$task_output:/evidence" ai-football-browser:v2.0.0-dev tests/browser/visual-regression.mjs
docker run --rm --network "$task_network" --cpus .5 --memory 768m --pids-limit 256 \
    -e MANUAL_BET_BASE_URL=http://web -e BROWSER_EVIDENCE_DIR=/evidence \
    -v "$task_root:/work:ro" -v "$task_output:/evidence" ai-football-browser:v2.0.0-dev tests/browser/manual-bet.mjs
docker image inspect "$task_image" --format '{{.Id}}' > "$task_output/image-id.txt"
