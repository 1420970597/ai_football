#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""乐鱼 App 账号登录续期：口令 → token → 场馆业务会话。

保留历史 App 请求格式；其成功条件不能从离线单元测试推断。
2026-10-08 正常网页登录实际返回 6022 并显示人机验证，历史抓包
`乐鱼app.zip` 当前不在仓库，不能据此宣称所有账户均无需验证。
本模块不处理或跳过人机验证。上游拒绝登录时停止后台账号重试，
网络失败才按冷却间隔重试；已有有效 token 可继续用于场馆续期。

6002/6008 只表示当前请求未获接受，不能排除请求协议不匹配。
登录签名必须属于 /site/api，场馆签名属于 /game/api。
配置来自环境变量，依赖仅标准库。
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
    "LoginRejected",
    "AppLoginClient",
    "AppLoginSessionProvider",
    "login_provider_from_env",
]

#: 登录端点（实测）
LOGIN_PATH = "/site/api/v1/user/login"

#: 登录凭据的环境变量名
LOGIN_ENV_NAME = "LEYU_APP_LOGIN_NAME"
LOGIN_ENV_PASSWORD = "LEYU_APP_LOGIN_PASSWORD"
#: /site/api 登录签名，与 LEYU_APP_SIGNATURE（/game/api）分开配置。
LOGIN_ENV_SIGNATURE = "LEYU_APP_LOGIN_SIGNATURE"

#: 客户端版本（上游会校验；低版本返回 6606「版本过低」）
CLIENT_VERSION = "2.0.1"

#: 站点 ID（实测 2001）
SITE_ID = "2001"

#: 历史 App 请求格式。不是网页验证码模式，也不保证当前上游接受。
KAPTC_TYPE_NO_CHECK = 99

#: 业务成功码
OK_STATUS = 6000

#: 「地区 IP 限制」业务码 —— 单列出来，便于给出可操作指引
IP_RESTRICTED = 6031

#: 「版本过低」业务码
VERSION_TOO_LOW = 6606


class LoginRejected(SessionError):
    """上游业务拒绝；同一 provider 不应自动重复提交账号口令。"""

    def __init__(self, status_code: str, detail: str) -> None:
        self.status_code = status_code
        super().__init__("登录被上游拒绝（status_code=%s）：%s" % (status_code, detail))


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
        return "name=<已配置> uuid=<已配置> pwd=<已配置>"


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
        #: 若提供，必须是 /site/api 的签名，不能复用场馆的 /game/api 签名。
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
            # 保留历史 App 格式；不能用于跳过当前网页验证流程。
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
            # 错误响应可能回显账号、token 或请求体，不写日志。
            self.last_error = "HTTP %d" % exc.code
            raise SessionError("登录请求失败：%s" % self.last_error) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.last_error = str(exc)[:160]
            raise SessionError("登录请求失败：%s" % self.last_error) from exc

        try:
            payload: Any = json.loads(raw)
        except ValueError as exc:
            self.last_error = "响应非 JSON"
            raise SessionError(self.last_error) from exc
        if not isinstance(payload, Mapping):
            self.last_error = "登录响应结构异常（非对象）"
            raise SessionError("登录响应结构异常（非对象）")

        status = payload.get("status_code")
        data = payload.get("data")
        if (not isinstance(status, (str, int)) or isinstance(status, bool)
                or not str(status).isdigit() or len(str(status)) > 8):
            self.last_error = "登录响应缺少有效 status_code"
            raise SessionError(self.last_error)
        code = str(status)
        if code != str(OK_STATUS):
            details = {
                "6002": "当前请求未获接受；不能仅凭状态码认定账号或口令错误。",
                "6008": "当前请求未获接受；不能仅凭状态码认定账号或口令错误。",
                "6003": "请求协议或签名未获接受，请核对 /site/api 登录请求。",
                "6022": "需要在官方页面完成人机验证，再提供有效 token。",
                "6030": "账号已触发登录失败次数限制，请按官方提示处理。",
                str(IP_RESTRICTED): "地区 IP 限制，请检查官方登录要求。",
                str(VERSION_TOO_LOW): "客户端版本过低，请核对当前 App 版本。",
            }
            detail = details.get(code, "请核对官方登录流程和请求协议。")
            self.last_error = "status_code=%s" % code
            raise LoginRejected(code, detail)

        token = ""
        if isinstance(data, Mapping):
            token = str(data.get("token") or "").strip()
        if not token:
            self.last_error = "登录成功但响应未包含 token"
            raise SessionError(self.last_error)
        self.last_error = ""
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
        self._login_rejection: Optional[LoginRejected] = None
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
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                os.fchmod(fh.fileno(), 0o600)
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

            if self._login_rejection is not None:
                raise SessionError(
                    "%s；已停止自动账号登录。请完成官方登录并更新 %s，"
                    "或修正配置后重建服务再试。"
                    % (self._login_rejection, APP_ENV_TOKEN))

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
        except SessionError as exc:
            self.last_error = self.client.last_error or str(exc)
            if isinstance(exc, LoginRejected):
                self._login_rejection = exc
            raise
        with self._lock:
            self._token = token
            self._token_at = time.time()
            self.logins += 1
            self.client.last_error = ""
            self._login_rejection = None
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

        保留尝试冷却和业务拒绝状态；invalidate 不能重新打开账号重试。
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
            "login_blocked": self._login_rejection is not None,
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
    `LEYU_APP_UUID`。登录签名使用 LEYU_APP_LOGIN_SIGNATURE（/site/api），
    场馆签名使用 LEYU_APP_SIGNATURE（/game/api），不能互相回退。

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
        signature=(e.get(LOGIN_ENV_SIGNATURE) or "").strip(),
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
