#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# != 2 || ! "$2" =~ ^[a-z0-9][a-z0-9_.-]*$ ]]; then
    printf '%s\n' 'Usage: start-managed-hermes-audit.sh <hermes container> <new audit container>' >&2
    exit 2
fi
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
launch=$1
target=$2
image=$(docker inspect --format '{{.Image}}' "$launch")
credentials=$(mktemp -- "$project_root/.runtime/.managed-audit-env.XXXXXX")
trap 'rm -f -- "$credentials"' EXIT
docker exec "$launch" /opt/hermes/.venv/bin/python -c '
import hmac
import os
token = hmac.digest(os.environ["HERMES_CONNECTION_TOKEN"].encode(), b"hermes-audit-snapshot-v1", "sha256").hex()
print("HERMES_AUDIT_SNAPSHOT_TOKEN=" + token)
' > "$credentials"
docker run -d --name "$target" --restart unless-stopped --network ai-football-v2_egress \
    --cpus .30 --memory 768m --pids-limit 96 --read-only --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /opt/data:rw,noexec,nosuid,nodev,size=128m,mode=0700,uid=10000,gid=10000 \
    --tmpfs /opt/data/.hermes:rw,noexec,nosuid,nodev,size=96m,mode=0700,uid=10000,gid=10000 \
    --tmpfs /tmp:rw,nosuid,nodev,size=64m,mode=1777 --user 10000:10000 \
    --env-file "$credentials" -e HERMES_HOME=/opt/data/.hermes -e HOME=/opt/data/home \
    -e PYTHONPATH=/managed-connection -e PYTHONDONTWRITEBYTECODE=1 \
    -v "$project_root/integrations/hermes:/managed-connection:ro" \
    --entrypoint sleep "$image" infinity
