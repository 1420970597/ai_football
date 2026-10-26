"""检查 Compose 渲染后的服务配置，输出不包含配置值。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


DATABASE = {"DATABASE_URL", "DATABASE_TRANSPORT", "SSH_TUNNEL_HOST"}
STORAGE = {"S3_ENDPOINT_URL", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET", "S3_REGION", "S3_USE_TLS", "S3_HTTPS_PROXY", "AWS_CA_BUNDLE", "S3_OBJECT_PREFIX"}
PERMITTED = {
    "storage-tunnel": {"STORAGE_TUNNEL_CONFIG"},
    "migrate": DATABASE,
    "api": DATABASE | STORAGE | {"APP_SESSION_SECRET", "DATA_ENCRYPTION_KEY", "EXPERT_MCP_TOKEN", "COLLECTOR_INTERNAL_TOKEN", "BOOTSTRAP_ADMIN_EMAIL", "BOOTSTRAP_ADMIN_PASSWORD", "BOOTSTRAP_ADMIN_TOTP_SECRET", "TELEGRAM_CONTROL_URL", "COOKIE_SECURE", "QUOTE_MAX_AGE_SECONDS"},
    "review-worker": DATABASE | STORAGE | {"HERMES_API_URL", "HERMES_API_MODEL", "HERMES_API_KEY", "REVIEW_WORKER_CONCURRENCY"},
    "collector": STORAGE | {"PLATFORM_API", "COLLECTOR_INTERNAL_TOKEN", "COLLECTOR_INTERVAL_SECONDS", "TELEGRAM_CONTROL_HOST", "TELEGRAM_CONTROL_PORT"},
    "hermes-mcp": {"PLATFORM_INTERNAL_API", "EXPERT_MCP_TOKEN", "MCP_HOST", "MCP_PORT"},
    "hermes": {"HERMES_HOME", "API_SERVER_ENABLED", "API_SERVER_KEY", "API_SERVER_HOST", "API_SERVER_PORT", "HERMES_MODEL", "HERMES_PROVIDER_BASE_URL", "HERMES_PROVIDER_API_KEY", "MCP_SERVER_URL", "EXPERT_MCP_TOKEN"},
    "web": set(),
}


def validate(document: dict, project_root: Path) -> list[str]:
    services = document.get("services", {})
    errors = []
    root = project_root.resolve()
    if set(services) != set(PERMITTED):
        errors.append("服务集合不符合当前运行契约")
    graph = {}
    for name, service in services.items():
        if service.get("env_file"):
            errors.append(f"{name}禁止整份环境注入")
        unexpected = set(service.get("environment", {})) - PERMITTED.get(name, set())
        if unexpected:
            errors.append(f"{name}出现非白名单配置键：{','.join(sorted(unexpected))}")
        if name != "web" and service.get("ports"):
            errors.append(f"{name}禁止向宿主发布端口")
        if service.get("privileged") or service.get("network_mode") == "host":
            errors.append(f"{name}禁止特权或宿主网络")
        if float(service.get("cpus", 0)) <= 0 or not service.get("mem_limit"):
            errors.append(f"{name}缺少资源限额")
        if not service.get("logging", {}).get("options", {}).get("max-size"):
            errors.append(f"{name}缺少日志空间限额")
        graph[name] = set(service.get("depends_on", {}))
        for volume in service.get("volumes", []):
            if volume.get("type") != "bind":
                errors.append(f"{name}不允许未审查的持久卷")
                continue
            if not volume.get("read_only"):
                errors.append(f"{name}配置挂载必须只读")
            source = Path(volume["source"]).resolve()
            if source != root and root not in source.parents:
                errors.append(f"{name}配置路径超出工作区")
    if "hermes" in services and "HERMES_PROVIDER_API_KEY" not in services["hermes"].get("environment", {}):
        errors.append("模型提供方配置应仅交 Hermes")
    for name in services:
        if name != "hermes" and "HERMES_PROVIDER_API_KEY" in services[name].get("environment", {}):
            errors.append(f"{name}不应获取模型提供方密钥")
    if services.get("api", {}).get("depends_on", {}).get("migrate", {}).get("condition") != "service_completed_successfully":
        errors.append("API 必须等待独立迁移完成")
    if "hermes" in graph.get("api", set()):
        errors.append("API 不得反向依赖 Hermes 形成启动等待")
    if services.get("api", {}).get("environment", {}).get("TELEGRAM_CONTROL_URL") != "http://collector:8092":
        errors.append("Telegram 控制路由须使用内部服务地址")
    for network in ("app-private", "storage-private"):
        if not document.get("networks", {}).get(network, {}).get("internal"):
            errors.append(f"{network}必须为内部网络")
    visiting, visited = set(), set()

    def walk(name):
        if name in visiting:
            errors.append(f"服务启动依赖存在循环：{name}")
            return
        if name in visited:
            return
        visiting.add(name)
        for target in graph.get(name, set()):
            if target not in graph:
                errors.append(f"{name}引用不存在的依赖服务")
            else:
                walk(target)
        visiting.remove(name)
        visited.add(name)

    for name in graph:
        walk(name)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", type=Path)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--fixture-env", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    for candidate in (args.document, args.fixture_env):
        if candidate is not None and root not in candidate.resolve().parents:
            print("检查输入必须位于项目内。", file=sys.stderr)
            return 2
    document = json.loads(args.document.read_text())
    errors = validate(document, root)
    if args.fixture_env:
        from reuse_inventory import parse_env
        expected = parse_env(args.fixture_env.read_text())
        for name in ("api", "collector", "review-worker"):
            # config 为可再次载入的 Compose 文档，美元符号会序列化为 $$。
            serialized = expected.get("S3_SECRET_KEY", "").replace("$", "$$")
            if document.get("services", {}).get(name, {}).get("environment", {}).get("S3_SECRET_KEY") != serialized:
                errors.append(f"{name}假配置特殊字符未按字面保留")
    for error in errors:
        print(error, file=sys.stderr)
    print("Compose 配置边界检查通过。" if not errors else "Compose 配置边界检查失败。")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
