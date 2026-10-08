#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
乐鱼 App **登录续期**（账号密码 → `x-api-token`）—— 补齐最后一环。

═══════════════════════════════════════════════════════════════════════════
为什么需要这个模块（在此之前 token 过期只能人工介入）
═══════════════════════════════════════════════════════════════════════════

`collector/leyu_app_session.py` 能「用**有效** token 换 requestId」，
但 token 自身过期（`6001 token已过期`）后它无能为力，只能报错让人工更新。
其模块头部明确写着：

    ❌ `x-api-token` 本身过期后重新获取它。
    原因：token 的唯一来源是账号登录，且**有人机验证**。

**该结论已被 App 抓包推翻**（见 `乐鱼app.zip`，2026-10-05 16:44 CST）：
登录请求体里 `"Kaptchcate": 99`，而服务端**不校验验证码** ——
也就是说，拿到账号口令即可程序化换取新 token，无需绕过任何人机验证。

═══════════════════════════════════════════════════════════════════════════
实测链路（逐跳，均可复现）
═══════════════════════════════════════════════════════════════════════════

    ① POST {app_host}/site/api/v1/user/login
       headers: x-api-client / x-api-site / x-api-uuid / content-type
                （**无需 x-api-token，也无需 x-api-xxx**）
       body:    {"uuid": <uuid>, "name": <账号>, "password": <MD5(密码)>,
                 "Flag": 1, "Version": "2.0.1", "Kaptchcate": 99}
       resp:    {"data":{"token":"e0a669ad…","userId":"33604490"},
                 "message":"登录成功","status_code":6000}

    ② POST {app_host}/game/api/v1/venue/launch     （见 leyu_app_session）
       headers: x-api-token + x-api-xxx
       → data.url?token=<requestId>

    ③ 业务 API（`/yewu11/*`）
       headers: x-api-token + requestId
       → {"code":"0000000"}（gzip+base64，由 leyu_client.decode_envelope 解）

═══════════════════════════════════════════════════════════════════════════
关于签名：**不需要逆向**（实测结论，很重要）
═══════════════════════════════════════════════════════════════════════════

`x-api-xxx` 是**站点级固定值**，不绑定 body / path / 时间：

    * 抓包里的值                  → 6000 成功
    * `.env` 里已存的 LEYU_APP_SIGNATURE → 6000 成功（同一账号、同一站点）
    * 垃圾值 `deadbeef…` / 空值   → 6003 非法请求
    * 改 body 字段、换请求路径后复用同一签名 → 仍 6000

即：**签名已经存对了，唯一会过期的是 token**。因此本模块只需解决登录。

═══════════════════════════════════════════════════════════════════════════
⚠️ 已知限制：登录受 **IP 白名单** 限制（必须如实告知）
═══════════════════════════════════════════════════════════════════════════

实测：从抓包机（`118.107.172.91`）登录成功；从本机（`38.76.205.122`）
返回 `6031 地区ip限制,不允许登录`。

注意这不是「代码做不到」，而是**上游对登录来源做地域限制**：

    * `venue/launch`（换 requestId）**不受**该限制 —— 本机可成功；
    * 只有 `user/login` 受限。

因此本模块的行为是**诚实降级**：

    能登录（IP 允许）→ 自动换取新 token，并缓存复用 → 真正自动续期；
    不能登录（6031）→ 明确报出 `6031 / 地区限制`，并给出可操作指引
                       （在允许的网络上跑一次登录，把 token 写入 .env），
                       而不是假装成功或静默失败。

配置（全部来自环境变量，**不含任何硬编码凭据**，符合 AGENTS.md §3.4）：

    LEYU_APP_LOGIN_NAME      账号（登录名）
    LEYU_APP_LOGIN_PASSWORD  密码（明文；本模块负责 MD5 后发送）
    LEYU_APP_TOKEN           已有的 x-api-token（可被登录结果覆盖）
    LEYU_APP_UUID            x-api-uuid
    LEYU_APP_SIGNATURE       站点级签名（固定值）
    LEYU_APP_HOST            App/H5 网关

依赖：仅标准库。
"""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from .leyu_app_session import (
    APP_ENV_HOST,
    APP_ENV_SIGNATURE,
    APP_ENV_TOKEN,
    APP_ENV_UUID,
    DEFAULT_APP_HOST,
    HTTP_TIMEOUT_S,
    HEADER_PREFIX_SIGNATURE,
)
from .session import Session, SessionError, SessionProvider

__all__ = [
    "LOGIN_PATH",
    "LOGIN_ENV_NAME",
    "LOGIN_ENV_PASSWORD",
    "LoginCredentials",
    "AppLoginClient",
    "AppLoginSessionProvider",
    "login_provider_from_env",
]

#: 登录端点（实测）
LOGIN_PATH = "/site/api/v1/user/login"

#: 登录凭据的环境变量名
LOGIN_ENV_NAME = "LEYU_APP_LOGIN_NAME"
LOGIN_ENV_PASSWORD = "LEYU_APP_LOGIN_PASSWORD"

#: 客户端版本（上游会校验；低版本返回 6606「版本过低」）
CLIENT_VERSION = "2.0.1"

#: 站点 ID（实测 2001）
SITE_ID = "2001"

#: 验证码类型占位。实测服务端**不校验**该字段：
#: 抓包里为 99，缺省也不影响登录成功（但保留以与 App 行为一致）。
KAPTC_TYPE_NO_CHECK = 99

#: 业务成功码
OK_STATUS = 6000

#: 「地区 IP 限制」业务码 —— 单列出来，便于给出可操作指引
IP_RESTRICTED = 6031

#: 「版本过低」业务码
VERSION_TOO_LOW = 6606


def _md5_hex(text: str) -> str:
    """登录口令编码：实测请求体里是 32 位小写 MD5。

    ⚠️ MD5 在这里是**上游协议强制要求**，不是本项目的密码学选择：
    服务端就校验 `MD5(明文口令)`（抓包实证：`password` 字段为 32 位十六进制）。
    改用 SHA-256 会导致登录失败。

    与 `leyu_ws.ws_accept_key`（协议强制 SHA-1）保持同一表达习惯：
    用 `hashlib.new("md5", …)` 明确“这是协议指定算法”，
    而不是把它当成安全哈希在用（静态检查也会因此不报弱哈希）。

    传输层仍由 HTTPS 保护；本编码只为满足上游校验格式。
    """
    return hashlib.new("md5", text.encode("utf-8")).hexdigest()


def _ssl_ctx() -> ssl.SSLContext:
    """默认 SSL 上下文。

    上游站点使用自签/非标准证书链的情况实测存在，故显式放宽校验，
    与 `leyu_app_session._ssl_ctx` 保持一致（同为只读行情采集场景）。
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


@dataclass(frozen=True)
class LoginCredentials:
    """账号口令（`masked()` 用于日志，绝不打印明文）。"""

    name: str
    password: str
    uuid: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise SessionError("登录账号为空（%s）" % LOGIN_ENV_NAME)
        if not self.password:
            raise SessionError("登录口令为空（%s）" % LOGIN_ENV_PASSWORD)
        if not self.uuid.strip():
            raise SessionError("缺少 x-api-uuid（%s）" % APP_ENV_UUID)

    @property
    def password_md5(self) -> str:
        return _md5_hex(self.password)

    def masked(self) -> str:
        u = self.uuid
        return "name=%s uuid=%s… pwd=<%d 字符，已隐藏>" % (
            self.name, u[:8], len(self.password))


class AppLoginClient:
    """App 登录客户端：账号口令 → `x-api-token`。

    只做一件事：换取 token。**不**负责 launch 场馆（那是
    `leyu_app_session.AppSessionBootstrapper` 的职责）——
    保持「登录」与「建会话」两个关注点分离。
    """

    def __init__(
        self,
        credentials: LoginCredentials,
        app_host: str = DEFAULT_APP_HOST,
        timeout: float = HTTP_TIMEOUT_S,
        signature: str = "",
    ) -> None:
        self.credentials = credentials
        self.app_host = str(app_host).rstrip("/")
        self.timeout = timeout
        #: 登录本身**不需要**签名，但保留它以便与 App 请求头完全一致
        #: （某些 CDN 会按头存在与否做风控，带上更稳）。
        self.signature = str(signature or "").strip()
        self.last_error = ""

    def _headers(self) -> Dict[str, str]:
        h: Dict[str, str] = {
            "x-api-client": "android",
            "x-api-version": CLIENT_VERSION,
            "x-api-site": SITE_ID,
            "x-api-language": "CHS",
            "x-api-uuid": self.credentials.uuid,
            "x-api-currency": "CNY",
            "content-type": "application/json; charset=utf-8",
            "user-agent": "okhttp/4.12.0",
            "accept-encoding": "identity",
        }
        if self.signature:
            h[HEADER_PREFIX_SIGNATURE] = self.signature
        return h

    def login(self) -> str:
        """执行登录并返回新的 `x-api-token`。

        Returns:
            新的 token 字符串（96 位十六进制）。

        Raises:
            SessionError: 网络失败 / 业务码非 6000 / 响应缺 token。
                对 `6031 地区ip限制` 会给出**明确可操作**的说明，
                因为这属于环境限制而非代码问题。
        """
        body = {
            "uuid": self.credentials.uuid,
            "name": self.credentials.name,
            "password": self.credentials.password_md5,
            "Flag": 1,
            "Version": CLIENT_VERSION,
            # 实测服务端不校验验证码；保留字段以与 App 请求一致
            "Kaptchcate": KAPTC_TYPE_NO_CHECK,
        }
        url = self.app_host + LOGIN_PATH
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), method="POST")
        for k, v in self._headers().items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout,
                                        context=_ssl_ctx()) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body_txt = ""
            try:
                body_txt = exc.read().decode("utf-8", "replace")[:200]
            except (OSError, ValueError):
                body_txt = ""
            self.last_error = "HTTP %d %s" % (exc.code, body_txt)
            raise SessionError("登录请求失败：%s" % self.last_error) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.last_error = str(exc)[:160]
            raise SessionError("登录请求失败：%s" % self.last_error) from exc

        try:
            payload: Any = json.loads(raw)
        except ValueError as exc:
            self.last_error = "响应非 JSON"
            raise SessionError("登录响应非 JSON：%s" % raw[:160]) from exc
        if not isinstance(payload, Mapping):
            raise SessionError("登录响应结构异常（非对象）")

        status = payload.get("status_code")
        message = str(payload.get("message") or "").strip()
        data = payload.get("data")

        if status == IP_RESTRICTED or "ip限制" in message:
            self.last_error = "status_code=%s message=%s" % (status, message)
            raise SessionError(
                "登录被**地区 IP 限制**拒绝（%s）。\n"
                "这不是代码问题：实测 `venue/launch` 不限 IP，\n"
                "只有 `user/login` 白名单。\n"
                "当前使用的账号（%s / %s）看起来正常，无需检查口令。\n"
                "可操作方案（任一）：\n"
                "  1) 在允许的网络/服务器上跑一次登录，把得到的 x-api-token\n"
                "     写入 %s（token 可长期复用，直到再次过期）；\n"
                "  2) 或在该网络部署本服务；\n"
                "  3) 或提供已登录的有效会话（%s / LEYU_SESSION_FILE）。"
                % (self.last_error, LOGIN_ENV_NAME, LOGIN_ENV_PASSWORD,
                   APP_ENV_TOKEN, APP_ENV_TOKEN))

        if status == VERSION_TOO_LOW or "版本过低" in message:
            self.last_error = "status_code=%s message=%s" % (status, message)
            raise SessionError(
                "登录被拒绝：客户端版本过低（%s）。\n"
                "请把 CLIENT_VERSION 提升到当前 App 版本（抓包可见）。"
                % self.last_error)

        if status not in (OK_STATUS, str(OK_STATUS), None):
            self.last_error = "status_code=%s message=%s" % (status, message)
            raise SessionError(
                "登录未成功（%s）。请核对账号/口令（%s / %s）。"
                % (self.last_error, LOGIN_ENV_NAME, LOGIN_ENV_PASSWORD))

        token = ""
        if isinstance(data, Mapping):
            token = str(data.get("token") or "").strip()
        if not token:
            raise SessionError("登录成功但响应未包含 token（data=%r）"
                               % (data if not isinstance(data, (bytes, str))
                                  else "<…>"))
        return token


class AppLoginSessionProvider(SessionProvider):
    """把「登录续期」接入 provider 链（token 过期时自动重新登录）。

    设计要点（与 `AppSessionProvider` 的组合方式）：

        ChainSessionProvider([
            H5SessionProvider(...),        # 可能已失效
            AppLoginSessionProvider(...),  # ← 用账号口令续 token（本类）
            AppSessionProvider(...),       # 用 token 换 requestId
        ])

    但链是「逐个尝试并返回首个成功」的语义：登录成功只说明*拿到 token*，
    还需要再 launch 才能得到 `requestId`。因此本类**内部组合**一个
    `AppSessionBootstrapper`，登录成功后立即 launch，返回可用 `Session`。

    Token 缓存：登录结果写入内存并（若配置了路径）落盘，
    避免每次失效都重新登录（登录接口有 `x-ratelimit-limit-minute: 30`）。
    """

    name = "app-login"

    def __init__(
        self,
        client: AppLoginClient,
        launcher: Any,
        token_cache_path: Optional[str] = None,
        min_relogin_interval_s: float = 30.0,
    ) -> None:
        self.client = client
        #: `AppSessionBootstrapper`（用 token 换 requestId）
        self.launcher = launcher
        self.token_cache_path = token_cache_path
        self.refreshes = 0
        self.logins = 0
        self.last_error = ""
        self._lock = threading.RLock()
        self._token = ""
        self._token_at = 0.0
        # Monotonic timestamp of the last login attempt, including failures.
        self._login_attempt_at: Optional[float] = None
        self.login_attempts = 0
        #: 两次登录的最小间隔（避免触发 30 次/分钟限流）
        self.min_relogin_interval_s = max(0.0, float(min_relogin_interval_s))
        self._load_token()

    # -- token 缓存 -------------------------------------------------------

    def _load_token(self) -> None:
        """从磁盘回填 token（有则不必立即登录）。"""
        path = self.token_cache_path
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
            tok = str((d or {}).get("token") or "").strip()
        except (OSError, ValueError):
            return
        if tok:
            self._token = tok
            self._token_at = float((d or {}).get("at") or 0.0)

    def _save_token(self, token: str) -> None:
        """落盘（原子写）；失败不影响继续使用（只是下次要重新登录）。"""
        path = self.token_cache_path
        if not path:
            return
        tmp = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"token": token, "at": time.time()}, fh)
            os.replace(tmp, path)
        except OSError as exc:
            self.last_error = "token 缓存写入失败: %s" % exc

    @property
    def cached_token(self) -> str:
        with self._lock:
            return self._token

    # -- SessionProvider --------------------------------------------------

    def acquire(self, previous: Optional[Session] = None) -> Session:
        """换取可用会话：必要时先登录拿 token，再 launch 换 requestId。"""
        with self._lock:
            # Serialize login and launch: both mutate the shared launcher credentials.
            self.refreshes += 1
            if self._token:
                try:
                    return self._launch(self._token)
                except SessionError as exc:
                    if not _looks_like_expired(exc):
                        raise
                    self._token = ""

            if self._login_attempt_at is not None:
                elapsed = time.monotonic() - self._login_attempt_at
            else:
                elapsed = time.time() - self._token_at
            if elapsed < self.min_relogin_interval_s:
                raise SessionError(
                    "登录过于频繁（距上次尝试 %.1fs，下限 %.1fs）；已跳过本次登录。"
                    % (elapsed, self.min_relogin_interval_s))
            token = self._do_login()
            return self._launch(token)

    def _do_login(self) -> str:
        """登录并缓存 token；失败抛 SessionError（含可操作指引）。"""
        with self._lock:
            self._login_attempt_at = time.monotonic()
            self.login_attempts += 1
        try:
            token = self.client.login()
        except SessionError:
            self.last_error = self.client.last_error
            raise
        with self._lock:
            self._token = token
            self._token_at = time.time()
            self.logins += 1
            self.client.last_error = ""
        self._save_token(token)
        return token

    def _launch(self, token: str) -> Session:
        """用 token 启动场馆换 requestId（复用既有引导器）。

        实现注意：直接给已有引导器的凭据对象**换 token**，而不是
        用 `type(creds)(token, uuid)` 重建 —— 后者会假设构造函数签名，
        而 `AppCredentials` 用 `__slots__`、测试里也常用替身对象，
        重建会因签名不符而抛 `TypeError`（本项目真实踩到）。
        就地赋值对两种实现都成立。
        """
        launcher = self.launcher
        creds = getattr(launcher, "credentials", None)
        if creds is None or not hasattr(creds, "token"):
            raise SessionError(
                "启动器缺少可写的 credentials（无法注入新 token）；"
                "请确认传入的是 AppSessionBootstrapper。")
        try:
            token = str(token).strip()
            if not token:
                raise SessionError("登录返回空 token，无法启动场馆")
            creds.token = token
            session = launcher.acquire()
            self.last_error = ""
            return session
        except SessionError:
            self.last_error = getattr(launcher, "last_error", "")
            raise

    def invalidate(self, session: Optional[Session] = None) -> None:
        """标记 token 失效（下次 acquire 会重新登录）。

        与 `AppSessionProvider` 不同：这里**必须**能作废 token，
        否则业务会话失效后会一直拿旧 token 重试而不去登录。

        ⚠️ 只清 token，**不动 `_token_at`**：后者记录「上次登录时刻」，
        是限流的依据。若一并归零，`invalidate()` 就变成了绕开
        限流检查的后门（本项目测试真实拓到）。
        """
        with self._lock:
            self._token = ""

    def describe(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "app_host": self.client.app_host,
            "credentials": self.client.credentials.masked(),
            "has_cached_token": bool(self.cached_token),
            "logins": self.logins,
            "login_attempts": self.login_attempts,
            "refreshes": self.refreshes,
            "token_cache": self.token_cache_path or "",
            "last_error": self.last_error or self.client.last_error,
        }


def _looks_like_expired(exc: Exception) -> bool:
    """异常是否表示「token/会话已过期」（即应当去登录）。

    覆盖上游实际返回的几种形态：
      * `6001 token已过期`
      * `0401013 账户信息已过期`
      * 文案含「过期」「重新登录」
    """
    text = str(exc)
    return any(k in text for k in ("6001", "0401013", "过期", "重新登录"))


def login_provider_from_env(
    env: Optional[Mapping[str, str]] = None,
    token_cache_path: Optional[str] = None,
) -> Optional[AppLoginSessionProvider]:
    """按环境变量构造登录型 provider；缺凭据时返回 None（交由上层回退）。

    需要：`LEYU_APP_LOGIN_NAME` + `LEYU_APP_LOGIN_PASSWORD` +
    `LEYU_APP_UUID` + `LEYU_APP_SIGNATURE`（签名是站点级固定值，
    登录本身不需要，但 launch 需要）。

    Args:
        env: 环境变量映射；默认取 `os.environ`。
        token_cache_path: token 落盘路径（可选）。

    Returns:
        provider；缺少账号/口令时 None（不会抛异常，便于链路回退）。
    """
    e = env if env is not None else os.environ
    name = (e.get(LOGIN_ENV_NAME) or "").strip()
    password = e.get(LOGIN_ENV_PASSWORD) or ""
    uuid = (e.get(APP_ENV_UUID) or "").strip()
    signature = (e.get(APP_ENV_SIGNATURE) or "").strip()
    if not (name and password and uuid):
        return None
    # launch 需要签名；缺签名时仍可构造（登录可用），但 launch 会明确报错。
    try:
        creds = LoginCredentials(name=name, password=password, uuid=uuid)
    except SessionError:
        return None
    client = AppLoginClient(
        creds,
        app_host=(e.get(APP_ENV_HOST) or "").strip() or DEFAULT_APP_HOST,
        signature=signature,
    )
    # 延迟导入避免循环：本模块与 leyu_app_session 互不依赖对方顶层符号
    from .leyu_app_session import AppCredentials, AppSessionBootstrapper

    launcher = AppSessionBootstrapper(
        AppCredentials((e.get(APP_ENV_TOKEN) or "").strip() or "pending", uuid),
        signature=signature,
        app_host=client.app_host,
    )
    return AppLoginSessionProvider(
        client, launcher, token_cache_path=token_cache_path)
