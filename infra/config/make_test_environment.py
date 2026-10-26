"""仅生成明显标注的假配置，供 Compose 解析验收，不连接任何服务。"""

from pathlib import Path

from reuse_inventory import render_compose_env


if __name__ == "__main__":
    root = Path.cwd().resolve()
    target = root / ".runtime" / "compose-test.env"
    target.parent.mkdir(mode=0o700, exist_ok=True)
    values = {
        "DATABASE_URL": "postgresql://fixture:fixture@storage-tunnel:15432/fixture",
        "DATABASE_TRANSPORT": "ssh",
        "S3_ENDPOINT_URL": "https://storage.example.invalid:9000",
        "S3_ACCESS_KEY": "fixture-only-access",
        "S3_SECRET_KEY": "fixture$with spaces'and\\slash\"",
        "S3_BUCKET": "fixture-only-bucket",
        "APP_SESSION_SECRET": "fixture-session",
        "DATA_ENCRYPTION_KEY": "fixture-encryption",
        "EXPERT_MCP_TOKEN": "fixture-mcp",
        "COLLECTOR_INTERNAL_TOKEN": "fixture-collector",
        "EXECUTOR_INTERNAL_TOKEN": "fixture-executor",
        "HERMES_API_KEY": "fixture-hermes-internal",
        "HERMES_PROVIDER_BASE_URL": "https://model.example.invalid/v1",
        "HERMES_PROVIDER_API_KEY": "fixture-model-provider",
    }
    target.write_text(render_compose_env(values), encoding="utf-8")
    target.chmod(0o600)
    print("已生成项目内假配置；仅供解析检查，不可用于启动业务。")
