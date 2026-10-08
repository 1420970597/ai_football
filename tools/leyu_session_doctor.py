#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""乐鱼会话链诊断：逐个 provider 试，报告谁成功、谁失败、为什么。

用途：会话不可用时**一眼定位**卡在哪一环（H5 cookie / App launch / 登录续期 /
命令 / 文件 / 环境变量），而不是只看到最终一句「鉴权失败」。

用法::

    python3 tools/leyu_session_doctor.py

只读：不会写入任何会话缓存，也不打印敏感值（只报长度与指纹）。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _report_ip_gate() -> None:
    """登录路径的 IP 闸门判定（6008=已放行 / 6031=被拦）。

    服务端**先校 IP 再校凭据**：用不存在的用户名打 `user/login`，
    拿到 6008「用户名或密码错误」即证明 IP 已过闸，此时只要凭据正确
    就能自动续期；拿到 6031 则是 IP 白名单问题，本机无法自解。

    复用 `tools/leyu_ip_probe.py` 的探针与判定，避免重复实现。
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from leyu_ip_probe import verdict, probe
    except ImportError as exc:  # pragma: no cover - 工具被单独拷贝时
        print(f"  (跳过：无法导入 leyu_ip_probe：{exc})")
        return
    res = probe(_gateway())
    kind, text = verdict(res)
    icon = "✓" if kind == "ip_ok" else "✗"
    if res.get("error"):
        print(f"  {icon} 请求未送达：{res['error']}")
    else:
        print(f"  {icon} HTTP {res.get('http')} → status_code={res.get('status_code')} "
              f"message={res.get('message')}")
    print(f"    判定: {kind} — {text.splitlines()[0]}")


def _gateway() -> str:
    """取乐鱼网关：优先 .env 的 LEYU_APP_HOST，否则用客户端默认。"""
    env = os.environ.get("LEYU_APP_HOST", "").strip()
    if env:
        return env
    from collector.leyu_app_login import DEFAULT_APP_HOST

    return DEFAULT_APP_HOST


def _disabled_credentials(env_file: str) -> dict:
    """找出 .env 里**被注释掉**的登录取凭据。

    为何要单独做（踩过的坑）：凭据常常已经填好，但整行被 `#` 注释掉
    （历史原因：早期以为本机 IP 被 6031 封禁，写了「需在允许的网络上使用」）。
    普通 .env 解析会跳过它，于是诊断显示 `<empty>` —— 用户看不出
    「东西就在那里、只是没启用」。

    Returns:
        `{键名: 值}`（仅注释行；供「这些值还能用吗」的验证）。
    """
    found: dict = {}
    try:
        with open(env_file, encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if not s.startswith("#") or "=" not in s:
                    continue
                body = s.lstrip("#").strip()
                if "=" not in body:
                    continue
                key, val = body.split("=", 1)
                key = key.strip()
                if (key.startswith("LEYU_") or key.endswith("_TOKEN")) and val.strip():
                    found[key] = val.strip()
    except OSError:
        pass
    return found


def _report_credentials(env: dict, commented: dict) -> None:
    """用生产登录客户端验证实际配置；只报告状态码，不输出响应或 token。"""
    from collector.leyu_app_login import (
        APP_ENV_HOST,
        APP_ENV_SIGNATURE,
        APP_ENV_UUID,
        DEFAULT_APP_HOST,
        LOGIN_ENV_NAME,
        LOGIN_ENV_PASSWORD,
        AppLoginClient,
        LoginCredentials,
    )
    from collector.session import SessionError

    name = (env.get(LOGIN_ENV_NAME) or commented.get(LOGIN_ENV_NAME) or "").strip()
    password = env.get(LOGIN_ENV_PASSWORD) or commented.get(LOGIN_ENV_PASSWORD) or ""
    uuid = (env.get(APP_ENV_UUID) or commented.get(APP_ENV_UUID) or "").strip()
    origin = "生效配置"
    if not env.get(LOGIN_ENV_NAME) or not env.get(LOGIN_ENV_PASSWORD):
        origin = "注释行（尚未注入运行环境）"
    if not (name and password):
        print("  – 未配置账号口令（自动登录续期不可用）")
        return
    if not uuid:
        print(f"  – 缺少 {APP_ENV_UUID}，无法按生产协议验证登录")
        return

    client = AppLoginClient(
        LoginCredentials(name=name, password=password, uuid=uuid),
        app_host=(env.get(APP_ENV_HOST) or DEFAULT_APP_HOST),
        signature=env.get(APP_ENV_SIGNATURE, ""),
    )
    print(f"  凭据来源：{origin}")
    try:
        token = client.login()
    except SessionError:
        import re

        match = re.search(r"status_code=([0-9]+)", client.last_error)
        code = match.group(1) if match else "network/protocol error"
        if code == "6031":
            print("  ✗ 登录被上游拒绝（6031，地区 IP 限制）")
        else:
            print(f"  ✗ 生产登录请求未成功（{code}）；该响应不足以判断账号或口令哪项有误")
        return

    # A successful token is intentionally discarded; this diagnostic is read-only.
    del token
    print("  ✓ 生产登录成功（token 未显示、未缓存）")


def _fp(value: str) -> str:
    """敏感值指纹：只暴露长度与首 4 位，够定位不同凭据、不足以复用。

    值短于 8 位时**不吐任何字符**：`value[:4]` 会把「短密钥」整串
    或大半串打出来（如 `abc` → `head=abc…`），那就不是脱敏了。
    短值本来也无区分风险，只报长度即可。
    """
    if not value:
        return "<empty>"
    if len(value) < 8:
        return f"len={len(value)} head=<略>…"
    return f"len={len(value)} head={value[:4]}…"


def main() -> int:
    from collector import session as S

    env = dict(os.environ)
    # 读 .env（项目约定），让诊断与真实运行一致
    env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        with open(env_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    except OSError:
        pass  # 没有 .env 也能跑：直接用进程环境

    print("=== 配置概览（脱敏）===")
    for k in ("LEYU_H5_SITE", "LEYU_H5_TOKEN", "LEYU_H5_UUID", "LEYU_H5_SIGNATURE",
              "LEYU_APP_TOKEN", "LEYU_APP_UUID", "LEYU_APP_SIGNATURE",
              "LEYU_APP_LOGIN_NAME", "LEYU_APP_LOGIN_PASSWORD",
              "LEYU_REQUEST_ID", "LEYU_SESSION_FILE"):
        print(f"  {k:26s} {_fp(env.get(k, ''))}")

    # 被注释掉的凭据：东西在那儿但没生效 —— 最常见的“看起来配好了却不工作”
    disabled = _disabled_credentials(env_file)
    if disabled:
        print("\n  ⚠ .env 里有**被注释掉**的凭据行（未生效）："
              f"{', '.join(sorted(disabled))}")
        print("    下方会用生产登录协议尝试验证；失败状态码不一定能区分账号与口令问题。")

    print("\n=== 生产登录校验（脱敏）===")
    _report_credentials(env, disabled)

    # 不启用缓存：我们要看**原始**链路，而不是被上次成功缓存掩盖
    env.pop(S.SESSION_ENV_CACHE, None)
    provider = S.make_session_provider(env=env)

    print("\n=== provider 链 ===")
    print(f"  {type(provider).__name__}")
    cur = provider
    depth = 0
    while isinstance(cur, S.ChainSessionProvider):
        for p in cur.providers:
            print(f"    [{depth}] {p.name:22s} {type(p).__name__}")
        depth += 1
        # 链里的每一项单独试，才能看出谁坏
        break

    if isinstance(provider, S.ChainSessionProvider):
        print("\n=== 逐项探测 ===")
        for p in provider.providers:
            try:
                s = p.acquire(None)
            except S.SessionError as exc:
                print(f"  ✗ {p.name:22s} {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - 诊断脚本要报出一切
                print(f"  ✗ {p.name:22s} {type(exc).__name__}: {exc}")
                continue
            print(f"  ✓ {p.name:22s} requestId={_fp(s.request_id)} "
                  f"host={s.host or '<default>'} note={s.note}")
    else:
        try:
            s = provider.acquire(None)
            print(f"\n  ✓ {provider.name} requestId={_fp(s.request_id)} note={s.note}")
        except Exception as exc:  # noqa: BLE001
            print(f"\n  ✗ {provider.name} {type(exc).__name__}: {exc}")

    # 用最终链做一次真实验证：拉赛程
    print("\n=== IP 闸门（登录路径）===")
    _report_ip_gate()

    print("\n=== 端到端验证（schedule）===")
    try:
        s = provider.acquire(None)
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 无法取得会话：{exc}")
        return 1
    try:
        from collector.leyu_client import LEYUClient

        # LEYUClient 不接受 Session 对象：它要的是解包后的字段。
        # 只在字段非空时传入，空值交给客户端自己的默认网关/Origin。
        kwargs: dict = {"request_id": s.request_id, "cookie": s.cookie}
        if s.host:
            kwargs["host"] = s.host
        if s.origin:
            kwargs["origin"] = s.origin
        if s.cuid:
            kwargs["cuid"] = s.cuid
        client = LEYUClient(**kwargs)
        matches = client.all_matches()
        print(f"  ✓ 赛程 OK：拿到 {len(matches)} 场")
        for m in matches[:3]:
            print(f"      · {m.mid} {m.tournament} {m.home} vs {m.away}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 赛程失败：{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
