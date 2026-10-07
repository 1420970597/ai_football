#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""乐鱼「地区 IP 限制」探针：判断**当前出口 IP** 能否通过 `user/login` 的 IP 闸门。

为什么需要这个脚本
------------------
`user/login` 受上游 **IP 白名单**限制（`collector/leyu_app_login.py` 的
`IP_RESTRICTED = 6031`）。历史上抓包机可过、本机被拒，因此换机/换 IP 后
必须先回答一个问题：**当前 IP 是被 IP 闸门拦住，还是凭据本身失效？**

两者都表现为「登录失败」，但处置完全相反：
  * `6031 地区ip限制`      → 换 IP / 加白名单（人工，非本机能解）
  * `6008 用户名或密码错误` → IP 已放行，问题在凭据（可自动续期）

判据（本脚本的核心逻辑）
------------------------
故意用**不存在的用户名 + 全零密码**打 `user/login`：

| 观测结果 | 含义 |
| --- | --- |
| `6031` / 含「ip限制」 | 当前 IP 被 IP 白名单拒绝 |
| `6008` 用户名或密码错误 | **IP 闸门已放行**（服务端先校验 IP，再校验凭据） |
| 其它业务码 | 见输出，需人工判读 |

因为服务端是「先 IP 后凭据」的顺序，所以拿到 `6008` 就证明 IP 已通。

用法
----
    python3 tools/leyu_ip_probe.py                 # 读 .env 的网关，打印判定
    python3 tools/leyu_ip_probe.py --json          # 只输出机器可读 JSON
    python3 tools/leyu_ip_probe.py --host https://xxx

仅使用标准库（宿主机无 pip 也能跑），与仓库其余零依赖工具一致。
脚本**不发送任何真实凭据**：用户名是占位串，密码是全零。
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

#: 与 collector/leyu_app_login 保持一致的常量（此处复制以免引入包依赖）
IP_RESTRICTED = 6031
BAD_CREDENTIALS = 6008
LOGIN_PATH = "/site/api/v1/user/login"
DEFAULT_TIMEOUT = 20.0
PROBE_NAME = "__ip_probe_nonexistent__"
PROBE_PASSWORD = "0" * 32

#: 出口 IP 查询端点（仅用于记录，失败不影响判定）
IP_ECHO_URL = "https://api.ipify.org?format=json"


def _ssl_ctx() -> ssl.SSLContext:
    """乐鱼网关用轮换域名 + 自签/不匹配证书，与采集侧保持同样宽松策略。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def load_env(path: str = ".env") -> Dict[str, str]:
    """读取 .env（不依赖 python-dotenv）。缺失文件返回空表。"""
    out: Dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def egress_ip(timeout: float = 8.0) -> str:
    """当前出口 IP（拿不到就返回空串，不影响主判定）。"""
    try:
        req = urllib.request.Request(IP_ECHO_URL,
                                     headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_ssl_ctx()) as resp:
            return str(json.loads(resp.read().decode("utf-8")).get("ip") or "")
    except Exception:
        return ""


def probe(host: str, timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """打一次 login，返回 `{http, status_code, message, raw}`。"""
    body = json.dumps({
        "x-api-name": PROBE_NAME,
        "x-api-password": PROBE_PASSWORD,
        # 实测服务端不校验验证码（Kaptchcate 只作字段占位）
        "Kaptchcate": 99,
    }, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        host.rstrip("/") + LOGIN_PATH, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "Dart/3.6 (dart:io)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_ssl_ctx()) as resp:
            raw = resp.read().decode("utf-8", "replace")
            http = int(resp.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        http = int(exc.code)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"http": 0, "status_code": None, "message": "",
                "error": "%s: %s" % (type(exc).__name__, exc)}

    payload: Dict[str, Any] = {}
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = {}
    return {
        "http": http,
        "status_code": payload.get("status_code"),
        "message": str(payload.get("message") or ""),
        "raw": raw[:200],
    }


def verdict(res: Dict[str, Any]) -> "tuple[str, str]":
    """把观测结果翻译成 `(判定码, 人读说明)`。"""
    code = res.get("status_code")
    msg = res.get("message") or ""
    if res.get("error"):
        return "unreachable", (
            "无法连接网关（%s）。先确认网关域名/端口是否仍然有效——"
            "乐鱼网关是轮换域名，失效时任何业务码都拿不到。" % res["error"])
    if code == IP_RESTRICTED or "ip限制" in msg:
        return "ip_restricted", (
            "当前 IP **被地区 IP 限制拒绝**（%s %s）。此闸门只作用于 "
            "`user/login`，`venue/launch` 不受影响。处置：换出口 IP / "
            "让上游加白名单；本机无法绕过。" % (code, msg))
    if code == BAD_CREDENTIALS:
        return "ip_ok", (
            "当前 IP **已通过 IP 闸门**（拿到 %s「%s」而非 %s）。"
            "服务端是先校验 IP 再校验凭据，故 IP 不是瓶颈；"
            "若采集失败，原因是凭据失效（见 token/ Cookie），"
            "此时可配置 LEYU_APP_LOGIN_NAME / LEYU_APP_LOGIN_PASSWORD "
            "启用自动续期。"
            % (code, msg, IP_RESTRICTED))
    return "unknown", (
        "未预期的响应（HTTP %s, status_code=%s, message=%s）。"
        "人工判读 raw 字段。" % (res.get("http"), code, msg))


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="乐鱼地区 IP 限制探针")
    ap.add_argument("--host", default="",
                    help="网关基地址；缺省读 .env 的 LEYU_APP_HOST / "
                         "LEYU_H5_SITE，再退到 collector 的实测默认值")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    env = load_env(args.env)
    host = (args.host or env.get("LEYU_APP_HOST") or env.get("LEYU_H5_SITE")
            or "https://www.rya9nr.vip:6502")
    if "://" not in host:
        host = "https://" + host

    ip = egress_ip()
    res = probe(host, timeout=args.timeout)
    code, explain = verdict(res)

    report = {"egress_ip": ip, "host": host, "probe": res,
              "verdict": code, "explain": explain}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("出口 IP   : %s" % (ip or "(未知)"))
        print("网关      : %s" % host)
        print("login 探测: HTTP %s status_code=%s message=%s"
              % (res.get("http"), res.get("status_code"), res.get("message")))
        print("判定      : %s" % code)
        print("说明      : %s" % explain)
    return 0 if code == "ip_ok" else 1


if __name__ == "__main__":
    sys.exit(main())
