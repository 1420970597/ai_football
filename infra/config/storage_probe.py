"""Bounded read-only storage checks using the active service environment.

Reports categories and booleans only. Never prints endpoints, users or secrets,
never lists a bucket root, and never uploads, deletes or migrates data.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit


def safe_prefix(value: str) -> str:
    if (not value or value.startswith("/") or not value.endswith("/") or len(value) > 160
            or any(part in {"", ".", ".."} for part in value[:-1].split("/"))
            or not re.fullmatch(r"[a-zA-Z0-9_./-]+", value)):
        raise ValueError("bounded_storage_prefix_required")
    return value


def error_category(error: BaseException) -> str:
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)) or "timeout" in type(error).__name__.lower():
        return "timeout"
    response = getattr(error, "response", {})
    if isinstance(response, dict):
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status in {401, 403}:
            return "authentication_or_permission_denied"
        if status == 404:
            return "bucket_or_object_not_found"
        if status == 429:
            return "rate_limited"
    if "ssl" in type(error).__name__.lower() or "certificate" in type(error).__name__.lower():
        return "tls_validation_failed"
    return type(error).__name__


def storage_acceptance_gate(report: dict) -> dict:
    reasons=[]
    if report.get("database",{}).get("state")!="read_only_verified":
        reasons.append("database_read_not_verified")
    if report.get("database",{}).get("least_privilege_verified") is not True:
        reasons.append("database_runtime_privileges_excessive_or_unknown")
    if report.get("objects",{}).get("state")!="read_only_verified":
        reasons.append("bounded_object_read_not_verified")
    return {"read_only_connectivity_verified": all(report.get(name,{}).get("state")=="read_only_verified" for name in ("database","objects")),
            "runtime_least_privilege_verified": not reasons,"blockers":reasons,
            "production_restore_verified":False}


async def database_probe(environment: dict[str, str]) -> dict:
    import asyncpg
    started = time.monotonic()
    dsn = environment.get("DATABASE_URL", "")
    if not dsn:
        return {"state": "unconfigured", "write_performed": False}
    conn = None
    try:
        parts = urlsplit(dsn)
        transport = environment.get("DATABASE_TRANSPORT", "tls")
        if transport == "ssh" and parts.hostname != environment.get("SSH_TUNNEL_HOST", "storage-tunnel"):
            raise ValueError("unverified_tunnel_target")
        if transport not in {"ssh", "tls"}:
            raise ValueError("unverified_database_transport")
        async with asyncio.timeout(10):
            conn = await asyncpg.connect(dsn, timeout=3, command_timeout=4,
                server_settings={"default_transaction_read_only": "on", "statement_timeout": "4000",
                                 "lock_timeout": "1000", "application_name": "platform-storage-readonly-probe"})
            async with conn.transaction(readonly=True):
                row = await conn.fetchrow(
                    "SELECT current_setting('server_version_num')::int AS server_version_num,"
                    "current_setting('transaction_read_only')='on' AS read_only,"
                    "r.rolsuper AS role_superuser,r.rolcreatedb AS role_creates_database,"
                    "has_schema_privilege(current_user,'public','CREATE') AS role_creates_in_public,"
                    "COALESCE((SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()),false) AS tls_in_use,"
                    "(SELECT count(*) FROM pg_class WHERE relnamespace='public'::regnamespace AND relkind='r' AND left(relname,3)='v2_') AS platform_tables,"
                    "(SELECT count(*) FROM pg_class WHERE relnamespace='public'::regnamespace AND relkind='r' AND left(relname,3)='v2_' AND relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)) AS platform_tables_owned"
                    " FROM pg_roles r WHERE rolname=current_user")
                result = dict(row)
                if transport == "tls" and not result["tls_in_use"]:
                    return {"state": "rejected", "category": "database_tls_not_active", "write_performed": False}
                result.update(state="read_only_verified", transport=transport, write_performed=False)
                result["least_privilege_verified"] = not any(result[key] for key in ("role_superuser", "role_creates_database", "role_creates_in_public", "platform_tables_owned"))
                return result
    except Exception as exc:
        return {"state": "failed", "category": str(exc) if isinstance(exc, ValueError) and str(exc).startswith("unverified_") else error_category(exc),
                "write_performed": False}
    finally:
        if conn is not None:
            await conn.close(timeout=2)


def object_probe(environment: dict[str, str]) -> dict:
    import boto3
    from botocore.config import Config
    if not all(environment.get(key) for key in ("S3_ENDPOINT_URL", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET")):
        return {"state": "unconfigured", "write_performed": False}
    started = time.monotonic()
    operation = "configuration"
    bucket_authenticated = False
    try:
        prefix = safe_prefix(environment.get("S3_OBJECT_PREFIX", "v2/"))
        endpoint = urlsplit(environment["S3_ENDPOINT_URL"])
        if endpoint.scheme != "https" or endpoint.username or endpoint.password:
            raise ValueError("verified_https_storage_required")
        ca = environment.get("AWS_CA_BUNDLE")
        if ca and not Path(ca).is_file():
            raise ValueError("storage_ca_unreadable")
        client = boto3.client("s3", endpoint_url=environment["S3_ENDPOINT_URL"],
            aws_access_key_id=environment["S3_ACCESS_KEY"], aws_secret_access_key=environment["S3_SECRET_KEY"],
            region_name=environment.get("S3_REGION", "us-east-1"), verify=ca or True,
            config=Config(connect_timeout=3, read_timeout=3, retries={"max_attempts": 0},
                proxies={"https": environment["S3_HTTPS_PROXY"]} if environment.get("S3_HTTPS_PROXY") else None))
        try:
            operation = "bucket_head"
            client.head_bucket(Bucket=environment["S3_BUCKET"])
            bucket_authenticated = True
            operation = "prefix_list"
            listed = client.list_objects_v2(Bucket=environment["S3_BUCKET"], Prefix=prefix, MaxKeys=1)
            return {"state": "read_only_verified", "tls_validation_enabled": True,
                    "bucket_authenticated": True, "prefix_limited": True,
                    "operation": operation,
                    "prefix_has_objects": bool(listed.get("Contents")), "write_permission_verified": False,
                    "write_performed": False, "elapsed_ms": round((time.monotonic() - started) * 1000, 1)}
        finally:
            client.close()
    except Exception as exc:
        return {"state": "failed", "category": str(exc) if isinstance(exc, ValueError) and str(exc) in {"bounded_storage_prefix_required", "verified_https_storage_required", "storage_ca_unreadable"} else error_category(exc),
                "operation": operation, "bucket_authenticated": bucket_authenticated,
                "write_performed": False, "elapsed_ms": round((time.monotonic() - started) * 1000, 1)}


async def run(environment: dict[str, str]) -> dict:
    started = time.monotonic()
    database, objects = await asyncio.gather(database_probe(environment), asyncio.to_thread(object_probe, environment))
    report={"checked_at": datetime.now(timezone.utc).isoformat(), "database": database, "objects": objects,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "production_restore_verified": False, "migration_performed": False, "deletion_performed": False}
    report["acceptance_gate"]=storage_acceptance_gate(report)
    return report


if __name__ == "__main__":
    report = asyncio.run(run(dict(os.environ)))
    print(json.dumps(report, sort_keys=True))
    raise SystemExit(0 if all(report[name]["state"] == "read_only_verified" for name in ("database", "objects")) else 1)
