"""只读盘点旧归档和 VPS 配置，秘密仅写工作区外保护目录。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
from typing import Any


def private_json(path: Path, document: Any) -> None:
    """创建时即限制权限，不经 stdout 输出敏感文档。"""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise ValueError("secret destination directory must be private")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def parse_env(document: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in document.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError("invalid environment entry")
        if key in values:
            raise ValueError("duplicate environment entry")
        value = value.strip()
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError("unterminated quoted environment value")
            quote = value[0]
            value = value[1:-1]
            if quote == "'":
                value = value.replace("\\'", "'")
            else:
                value = json.loads('"' + value + '"')
        if "\0" in value:
            raise ValueError("invalid environment value")
        values[key] = value
    return values


def render_compose_env(values: dict[str, str]) -> str:
    """单引号禁止 Compose 对美元符号求值；配置永不作为 shell 执行。"""
    lines = []
    for key, value in values.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or any(char in value for char in "\r\n\0"):
            raise ValueError("invalid environment key or multiline value")
        escaped = value.replace("'", "\\'")
        lines.append(f"{key}='{escaped}'")
    return "\n".join(lines) + "\n"


def summarize(document: Any) -> Any:
    """只暴露结构与是否有值；所有字符串/数字值均不回显。"""
    if isinstance(document, dict):
        return {str(key): summarize(value) for key, value in document.items()}
    if isinstance(document, list):
        return {"type": "list", "count": len(document), "item_keys": sorted({str(key) for item in document if isinstance(item, dict) for key in item})}
    return {"type": type(document).__name__, "present": bool(document)}


class Remote:
    def __init__(self, ssh_config: str, host: str):
        self.ssh_config = ssh_config
        self.host = host

    def read(self, command: str) -> str:
        result = subprocess.run(
            ["ssh", "-F", self.ssh_config, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", self.host, command],
            text=True, capture_output=True, timeout=75, check=False,
        )
        if result.returncode:
            raise RuntimeError(f"remote read failed (exit {result.returncode})")
        return result.stdout

    def inspect(self, container: str) -> dict[str, Any]:
        return json.loads(self.read("docker inspect " + shlex.quote(container)))[0]


def archive_inventory(archive: Path, destination: Path) -> dict[str, Any]:
    restored: list[dict[str, Any]] = []
    samples = (
        "ai_football/.env", "ai_football/collector/leyu_client.py",
        "ai_football/store/runtime_settings_db.py", "ai_football/service/live_expert.py",
        "ai_football/docker/docker-compose.storage.yml",
    )
    with tarfile.open(archive) as source:
        names = set(source.getnames())
        for name in samples:
            if name not in names:
                restored.append({"source": name, "found": False})
                continue
            member = source.getmember(name)
            if not member.isfile() or member.size > 5_000_000:
                raise ValueError("sample is not a bounded regular file")
            content = source.extractfile(member).read()
            target = destination / "recovery-samples" / name.removeprefix("ai_football/")
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
            expected = hashlib.sha256(content).hexdigest()
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise ValueError("sample restore checksum mismatch")
            restored.append({"source": name, "found": True, "bytes": len(content), "sha256": expected, "restored_equal": True})
        legacy_env = parse_env(source.extractfile("ai_football/.env").read().decode())
        allowed = {key: value for key, value in legacy_env.items() if key.startswith(("PG_", "PI_LLM_", "LEYU_"))}
        private_json(destination / "legacy-environment.json", allowed)
        result = {"member_count": len(names), "restored_samples": restored, "environment": summarize(allowed)}
        private_json(destination / "archive-inventory.json", result)
    return result


def remote_inventory(remote: Remote, destination: Path) -> dict[str, Any]:
    api = remote.inspect("ai_football_storage_api")
    database = remote.inspect("ai-football-postgres")
    objects = remote.inspect("ai-football-s3-rustfs-1")
    snapshots = {"api": api, "database": database, "objects": objects}
    private_json(destination / "legacy-remote-containers.json", snapshots)
    api_env = dict(item.split("=", 1) for item in api["Config"]["Env"] if "=" in item)
    db_env = dict(item.split("=", 1) for item in database["Config"]["Env"] if "=" in item)
    obj_env = dict(item.split("=", 1) for item in objects["Config"]["Env"] if "=" in item)
    database_name = db_env.get("POSTGRES_DB", db_env.get("POSTGRES_USER", "postgres"))
    database_user = db_env.get("POSTGRES_USER", "postgres")
    def sql(query: str) -> list[Any]:
        command = "docker exec ai-football-postgres psql -X -q -t -A -U " + shlex.quote(database_user) + " -d " + shlex.quote(database_name) + " -v ON_ERROR_STOP=1 -c " + shlex.quote("BEGIN READ ONLY; SET LOCAL statement_timeout='5000ms'; " + query + "; COMMIT")
        output = remote.read(command)
        return [json.loads(line) for line in output.splitlines() if line.startswith(("{", "["))]
    settings_rows = sql("SELECT row_to_json(s) FROM (SELECT name,version,settings FROM runtime_settings ORDER BY name LIMIT 10) s")
    server = sql("SELECT json_build_object('server_version',current_setting('server_version'),'ssl_enabled',current_setting('ssl'),'database_size',pg_database_size(current_database()),'read_only',current_setting('transaction_read_only'))")
    table_names = sql("SELECT json_agg(table_name ORDER BY table_name) FROM information_schema.tables WHERE table_schema='public'")
    private_json(destination / "legacy-runtime-settings.json", settings_rows)
    private_json(destination / "legacy-remote-env.json", {"api": api_env, "objects": obj_env})
    result = {
        "containers": {key: {"running": value["State"]["Running"], "healthy": value["State"].get("Health", {}).get("Status"), "image": value["Config"]["Image"], "mount_targets": [m["Destination"] for m in value["Mounts"]], "environment_keys": sorted(x.split("=", 1)[0] for x in value["Config"]["Env"])} for key, value in snapshots.items()},
        "runtime_settings": [{"version": row["version"], "structure": summarize(row["settings"])} for row in settings_rows],
        "server": server,
        "table_names": table_names,
    }
    private_json(destination / "remote-inventory.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("archive", "remote"))
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--ssh-config")
    parser.add_argument("--ssh-host", default="leyu-storage")
    args = parser.parse_args()
    try:
        result = archive_inventory(args.archive, args.destination) if args.mode == "archive" else remote_inventory(Remote(args.ssh_config, args.ssh_host), args.destination)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
