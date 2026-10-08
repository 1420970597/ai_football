#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断会话与赛程；默认不提交账号口令。

python3 tools/leyu_session_doctor.py          # token / 文件 / 环境会话诊断
python3 tools/leyu_session_doctor.py --login  # 显式允许一次账号登录或登录命令

采用生产 provider，复用已取得的会话验证赛程，不重复 acquire。
本工具不写会话缓存；--login 会向上游提交登录请求，可能更新上游会话。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _disabled_credentials(env_file: str) -> dict:
    """只标记被注释的配置；不会启用或提交这些凭据。"""
    found: dict = {}
    try:
        with open(env_file, encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if not s.startswith("#") or "=" not in s:
                    continue
                key, val = s.lstrip("#").strip().split("=", 1)
                key = key.strip()
                if (key.startswith("LEYU_") or key.endswith("_TOKEN")) and val.strip():
                    found[key] = val.strip()
    except OSError:
        pass
    return found


def _environment(env_file: str) -> dict:
    """使用 Compose 的 .env 语义，避免引号/变量展开改变实际口令。"""
    env = dict(os.environ)
    if not os.path.exists(env_file):
        return env
    root = os.path.dirname(env_file)
    try:
        raw = subprocess.check_output([
            "docker", "compose", "--env-file", env_file,
            "-f", os.path.join(root, "docker", "docker-compose.yml"),
            "config", "--format", "json",
        ], stderr=subprocess.DEVNULL, text=True)
        configured = json.loads(raw)["services"]["analytics-api"]["environment"]
        if not isinstance(configured, dict):
            raise TypeError("invalid environment")
        for key, value in configured.items():
            env.setdefault(key, str(value or ""))
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError, TypeError):
        print("  – 无法按 Compose 解析 .env；仅诊断当前进程环境")
    return env


def _report_credentials(env: dict, commented: dict, allow_login: bool = False) -> None:
    """仅报告配置是否生效，不单独发送登录请求。"""
    name = env.get("LEYU_APP_LOGIN_NAME")
    password = env.get("LEYU_APP_LOGIN_PASSWORD")
    if not (name and password):
        if commented.get("LEYU_APP_LOGIN_NAME") or commented.get("LEYU_APP_LOGIN_PASSWORD"):
            print("  – 账号口令仅在注释行中，未生效；不会自动启用或尝试")
        else:
            print("  – 未配置账号口令（自动登录续期不可用）")
    elif not env.get("LEYU_APP_UUID"):
        print("  – 缺少 LEYU_APP_UUID，无法使用生产登录协议")
    elif allow_login:
        print("  – 生效配置将在 provider 探测中至多尝试一次账号登录")
    else:
        print("  – 账号口令已配置；本次未提交（使用 --login 才允许账号登录）")


def _fp(value: str) -> str:
    """可比对的 token 指纹，不显示原值的任何字符。"""
    if not value:
        return "<empty>"
    return "len=%d sha256=%s" % (len(value), hashlib.sha256(value.encode()).hexdigest()[:8])


def _error(exc: Exception) -> str:
    """上游异常可能回显凭据，只报告类型和业务码。"""
    match = re.search(r"status_code=([0-9]{1,8})(?![0-9])", str(exc))
    return type(exc).__name__ + (" status_code=" + match.group(1) if match else "")


def main(argv: Optional[List[str]] = None) -> int:
    from collector import session as S

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login", action="store_true", help="允许一次生产账号登录或登录命令")
    args = parser.parse_args(argv)
    env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    env = _environment(env_file)
    print("=== 配置概览（脱敏）===")
    for key in ("LEYU_H5_SITE", "LEYU_H5_TOKEN", "LEYU_H5_UUID", "LEYU_H5_SIGNATURE",
                "LEYU_APP_HOST", "LEYU_APP_TOKEN", "LEYU_APP_UUID", "LEYU_APP_SIGNATURE",
                "LEYU_APP_LOGIN_SIGNATURE", "LEYU_APP_SIGNING_CONFIG",
                "LEYU_APP_LOGIN_NAME", "LEYU_APP_LOGIN_PASSWORD",
                "LEYU_REQUEST_ID", "LEYU_SESSION_FILE"):
        value = env.get(key, "")
        fingerprint = ("<已配置>" if value else "<empty>") if key in (
            "LEYU_APP_LOGIN_NAME", "LEYU_APP_LOGIN_PASSWORD") else _fp(value)
        print(f"  {key:26s} {fingerprint}")
    disabled = _disabled_credentials(env_file)
    if disabled:
        print("  – 注释配置（未生效）：" + ", ".join(sorted(disabled)))
    _report_credentials(env, disabled, allow_login=args.login)

    if not args.login:
        env.pop("LEYU_APP_LOGIN_NAME", None)
        env.pop("LEYU_APP_LOGIN_PASSWORD", None)
        env.pop(S.SESSION_ENV_COMMAND, None)
    provider = S.make_session_provider(env=env)
    # 跳过业务会话的历史回退，但保留生产 token 缓存与持久化登录保护。
    if isinstance(provider, S.CachedSessionProvider):
        provider = provider.inner
    providers = provider.providers if isinstance(provider, S.ChainSessionProvider) else [provider]
    session: Optional[S.Session] = None
    print("\n=== provider 探测（每项一次）===")
    for item in providers:
        try:
            acquired = item.acquire(None)
        except Exception as exc:  # noqa: BLE001 - 诊断不得泄漏原始错误
            print(f"  ✗ {item.name:22s} {_error(exc)}")
            continue
        if session is None:
            session = acquired
        print(f"  ✓ {item.name:22s} requestId={_fp(acquired.request_id)}")

    print("\n=== 端到端验证（复用会话，不再次登录）===")
    if session is None:
        print("  ✗ 无可用会话")
        return 1
    try:
        from collector.leyu_client import LEYUClient

        kwargs: dict = {"request_id": session.request_id, "cookie": session.cookie}
        for key in ("host", "origin", "cuid"):
            value = getattr(session, key)
            if value:
                kwargs[key] = value
        matches = LEYUClient(**kwargs).all_matches()
        print(f"  ✓ 赛程 OK：拿到 {len(matches)} 场")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 赛程失败：{_error(exc)}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
