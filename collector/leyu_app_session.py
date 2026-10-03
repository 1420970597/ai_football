#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
乐鱼 App 会话引导（`x-api-token` → `requestId`）—— 解决「requestId 从哪来」。

═══════════════════════════════════════════════════════════════════════════
这条链路解决了此前缺失的最后一环
═══════════════════════════════════════════════════════════════════════════

之前无法打通的原因：业务 API（`api.*` / `/yewu11/*`）需要 `requestId`，
而 `requestId` 既不是随机值、也不来自 `x-api-token` 直换。

实测发现 `requestId` 是**通过「启动场馆」换来的**：

    iOS/Android App
      │  POST /game/api/v1/venue/launch   {"enName":"YBTY", ...}
      │  headers: x-api-token / x-api-uuid / x-api-xxx（按前缀的签名）
      ▼
    {"data":{"url":"https://api.z0ugnx7k.com?token=38bb7a74…","h5Url":...}}

    token 即业务 API 的 requestId：
      GET https://api.z0ugnx7k.com/yewu11/v2/m/getOriginalDataPB
      header requestId: 38bb7a74…
      → {"code":"0000000"}  data=431544 字节  ✅ 实测成功

    同一 token 也是 H5 入口：
      https://app-h5.ztczzx.com?token=38bb7a74…&api=<加密的网关列表>

场馆代号（实测）：
    YBTY  乐鱼体育    → 启动成功，指向 api.* 业务网关（**本模块使用**）
    OBTY  乐鱼体育(旧) → 「即将开放，敬请期待」
    IMTY  电竞        → 启动成功，指向第三方平台
    YBBY  捕鱼        → 启动成功

═══════════════════════════════════════════════════════════════════════════
签名 `x-api-xxx` 的实测结论（重要，避免过度设计）
═══════════════════════════════════════════════════════════════════════════

App 与 PC 都发送 `x-api-xxx`，其取值是**按 API 前缀分组的一张表**：

    {"/site/api":"19559a3d…", "/act/api":"f65397cb…",
     "/game/api":"7eb35562…", "/fd/api":"80254b96…", …}

该表在浏览器里以 `localStorage.uuidToBase64` 存储，加密方式为
**AES-256-CBC**，密钥 `ZFRYCMdFYGf0i5HgO0oWvFV0terUABU0`、IV `CbE3P3t1lY34Ns8F`
（PKCS7）—— 解出来即上面这张表。

实测**校验强度**（对 `/game/api/v1/venue/launch` 逐项对照）：

    | x-api-xxx              | 结果 |
    | 正确的 /game/api 值    | ✅ 场馆启动成功 |
    | 全 0 占位              | ❌ 6003 非法请求 |
    | 空字符串               | ❌ 6003 非法请求 |
    | 随机 64 位 hex         | ❌ 6003 非法请求 |
    | 错前缀（/site/api 值） | ❌ 6003 非法请求 |

即：**该签名必须正确**。不同端点校验强度不同
（`/site/api/v1/site/venue/sort` 不校验，`venue/launch` 校验）。

进一步实测**绑定性**（同一 `x-api-xxx` 配不同 uuid/token）：

    | uuid     | token   | 结果 |
    | 原值     | 原值    | ✅ 成功 |
    | 随机 uuid| 原 token| ✅ 成功 |
    | 空 uuid  | 原 token| ✅ 成功 |
    | 原 uuid  | 随机token| ❌ 6001 token已过期 |

结论：**签名既不绑定 uuid、也不绑定 token，是站点级的固定值**。
因此它可以被抓取一次后**长期缓存复用**，这是自动续期可行的关键。

本模块**不实现签名算法**（不逆向其加密逻辑），而是接受一个由运维/
一次性抓取提供的签名值；若缺失则给出可操作报错。

═══════════════════════════════════════════════════════════════════════════
会话续期
═══════════════════════════════════════════════════════════════════════════

`launch()` 每次调用都返回**新的** `requestId`，因此续期 = 重新 launch。
`x-api-token` 本身的有效性由上游决定；失效时 `launch` 会返回业务错误，
本模块将其转换为 `SessionError`，交由 `collector.session` 的 provider 链处理。

本模块**不实现登录**：`x-api-token` 必须由运维注入（环境变量或文件）。
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urlsplit, parse_qs

from .session import Session, SessionError, SessionProvider

__all__ = [
    "VENUE_YBTY",
    "HEADER_PREFIX_SIGNATURE",
    "AppSessionBootstrapper",
    "AppSessionProvider",
    "AppCredentials",
    "bootstrapper_from_env",
    "APP_ENV_TOKEN",
    "APP_ENV_UUID",
    "APP_ENV_HOST",
]

#: 场馆代号 → 说明（实测）
VENUE_YBTY = "YBTY"    # 乐鱼体育，指向 api.* 业务网关 —— 本项目使用
VENUE_OBTY = "OBTY"    # 乐鱼体育(旧)，实测「即将开放」
VENUE_IMTY = "IMTY"    # 电竞，指向第三方平台
VENUE_YBBY = "YBBY"    # 捕鱼

#: 前缀签名请求头
HEADER_PREFIX_SIGNATURE = "x-api-xxx"

#: 签名环境变量（站点级固定值；实测不绑定 uuid/token，可长期缓存）
APP_ENV_SIGNATURE = "LEYU_APP_SIGNATURE"

#: App 凭据的环境变量名
APP_ENV_TOKEN = "LEYU_APP_TOKEN"     # x-api-token（运维注入，**不入库**）
APP_ENV_UUID = "LEYU_APP_UUID"       # x-api-uuid
APP_ENV_HOST = "LEYU_APP_HOST"       # App/H5 网关，默认取实测值


#: 实测可用的 App/H5 网关（承载 /game/api 与 /site/api）
DEFAULT_APP_HOST = "https://www.rya9nr.vip:6502"

#: 请求超时（秒）
HTTP_TIMEOUT_S = 25.0

#: launch 请求体（字段取自前端 chunk 实测）
_LAUNCH_BODY: Mapping[str, Any] = {
    "enName": VENUE_YBTY,
    "appSendMoney": False,
    "activity": True,
    "gameCode": "",
    "https": True,
    "isApp": False,
    "isSure": "",
    "isPreload": True,
    "isManualLaunch": True,
    "siteId": "2001",
    "temporaryParam": "",
}

#: 会话有效期（秒）：launch 换来的 token 实测数小时内有效，取保守值
SESSION_TTL_S = 3600.0


class AppCredentials:
    """App 侧凭据（x-api-token / x-api-uuid）。绝不写日志、绝不持久化。"""

    __slots__ = ("token", "uuid")

    def __init__(self, token: str, uuid: str) -> None:
        if not str(token).strip():
            raise SessionError("缺少 x-api-token（%s）" % APP_ENV_TOKEN)
        if not str(uuid).strip():
            raise SessionError("缺少 x-api-uuid（%s）" % APP_ENV_UUID)
        self.token = token.strip()
        self.uuid = uuid.strip()

    def masked(self) -> str:
        return "token=%s…%s uuid=%s" % (
            self.token[:6], self.token[-4:], self.uuid)

    def __repr__(self) -> str:  # pragma: no cover - 防误打印
        return "AppCredentials(%s)" % self.masked()


def _safe_base_url(url: str) -> str:
    """校验并规范化基地址，**只允许 http/https**。

    urllib 会接受 `file://` 等 scheme；基地址来自环境变量配置，
    若不加限制，一个被篡改的配置就可能变成任意本地文件读取。
    """
    candidate = str(url or "").strip().rstrip("/")
    scheme = urlsplit(candidate).scheme.lower()
    if scheme not in ("http", "https"):
        raise SessionError(
            "App 网关必须是 http/https URL，实际为 %r" % (candidate[:80],))
    return candidate


def _ssl_ctx() -> ssl.SSLContext:
    """宽松 TLS 上下文。

    这些网关使用轮换域名 + 自签/不匹配证书，严格校验会导致全部请求失败。
    仅用于读取公开赔率数据，不传输任何敏感信息（令牌只发往上游自身）。
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class AppSessionBootstrapper:
    """用 App 凭据换取业务 API 的 `requestId`（即 Session）。

    Args:
        credentials: App 凭据。
        app_host: App/H5 网关（承载 `/game/api`）。
        venue: 场馆代号，默认 YBTY（乐鱼体育）。
        timeout: 请求超时。
    """

    def __init__(
        self,
        credentials: AppCredentials,
        signature: str = "",
        app_host: str = DEFAULT_APP_HOST,
        venue: str = VENUE_YBTY,
        timeout: float = HTTP_TIMEOUT_S,
    ) -> None:
        self.credentials = credentials
        self.signature = str(signature).strip()
        self.app_host = _safe_base_url(app_host)
        self.venue = venue
        self.timeout = timeout
        self.last_error = ""

    # -- HTTP -------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        if not self.signature:
            self.last_error = "缺少 %s" % APP_ENV_SIGNATURE
            raise SessionError(
                "缺少前缀签名 %s（%s）。\n"
                "该值是**站点级的固定值**（实测不绑定 uuid/token，可长期缓存）。\n"
                "获取方式：从浏览器 localStorage 的 uuidToBase64 取出后 AES 解密，\n"
                "或直接抓一次 App 的 venue/launch 请求复制其 x-api-xxx。"
                % (HEADER_PREFIX_SIGNATURE, APP_ENV_SIGNATURE))
        return {
            "x-api-client": "sport_android",
            "x-api-version": "2.0.1",
            "x-api-site": "2001",
            "x-api-language": "CHS",
            "x-api-uuid": self.credentials.uuid,
            "x-api-token": self.credentials.token,
            "x-api-currency": "CNY",
            HEADER_PREFIX_SIGNATURE: self.signature,
            "content-type": "application/json;charset=utf-8",
            "user-agent": "okhttp/4.12.0",
            "accept-encoding": "identity",
        }

    def _post(self, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        url = self.app_host + path
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), method="POST")
        for k, v in self._headers().items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout,
                                        context=_ssl_ctx()) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            self.last_error = "HTTP %d" % exc.code
            raise SessionError("场馆启动失败（%s）: %s" % (self.last_error, url)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.last_error = str(exc)[:120]
            raise SessionError("场馆启动请求失败: %s" % self.last_error) from exc
        try:
            return json.loads(raw)
        except ValueError as exc:
            self.last_error = "响应非 JSON"
            raise SessionError("场馆启动响应非 JSON: %s" % raw[:120]) from exc

    # -- 主流程 -----------------------------------------------------------

    def acquire(self) -> Session:
        """启动场馆，把返回 URL 里的 token 作为 `requestId`。

        Returns:
            可用的 Session（`request_id` = 业务网关 token，`host` = 该 token 绑定的网关）。
        Raises:
            SessionError: 凭据失效、场馆维护、或响应缺少 token。
        """
        body = dict(_LAUNCH_BODY)
        body["enName"] = self.venue
        payload = self._post("/game/api/v1/venue/launch", body)

        # 业务码：6000 成功；其它（如 6005 即将开放 / 6415 维护）为失败
        status_code = payload.get("status_code")
        message = str(payload.get("message") or "").strip()
        data = payload.get("data") or {}
        if status_code not in (6000, "6000", None) or not isinstance(data, Mapping):
            self.last_error = "status_code=%s message=%s" % (status_code, message)
            raise SessionError(
                "场馆 %s 启动未成功（%s）。"
                "若是凭据失效，请更新 %s 后重试；"
                "若是场馆维护/未开放，可改试其它场馆代号。"
                % (self.venue, self.last_error, APP_ENV_TOKEN))

        launch_url = str(data.get("url") or "")
        if not launch_url:
            self.last_error = "响应缺少 data.url"
            raise SessionError("场馆 %s 启动成功但未返回 url" % self.venue)

        parts = urlsplit(launch_url)
        request_id = (parse_qs(parts.query).get("token") or [""])[0]
        if not request_id:
            self.last_error = "url 中缺少 token 参数"
            raise SessionError(
                "场馆 %s 的 url 未携带 token（无法取得 requestId）: %s"
                % (self.venue, launch_url[:120]))

        host = "%s://%s" % (parts.scheme or "https", parts.netloc)
        return Session(
            request_id=request_id,
            # cuid 由 App 端点使用；业务 API 的 cuid 可留空
            cuid="",
            host=host,
            origin=str(data.get("h5Url") or "").split("?")[0] or "",
            ttl_s=SESSION_TTL_S,
            note="来自 App 场馆启动（%s @ %s）" % (self.venue, host),
        )

    def describe(self) -> Dict[str, Any]:
        # 引导器不是 SessionProvider，没有 name；用 bootstrapper 字段标识自己。
        return {
            "bootstrapper": "app-launch",
            "venue": self.venue,
            "app_host": self.app_host,
            "credentials": self.credentials.masked(),
            "last_error": self.last_error,
        }


class AppSessionProvider(SessionProvider):
    """`SessionProvider` 适配器：把 App 引导器接入 provider 链。

    这样 `collector.sources.LEYUSource` 的失效自动续期（0401013 →
    重新 acquire）会自动走「重新 launch 场馆」，无需改动数据源层。

    继承 `SessionProvider` 以获得默认的 `invalidate`（无操作）：
    场馆启动换来的 token 是一次性的，服务端不保留可作废的会话状态，
    因此失效时只需重新 acquire 即可。
    """

    name = "app-launch"

    def __init__(self, bootstrapper: AppSessionBootstrapper) -> None:
        self.bootstrapper = bootstrapper
        self.refreshes = 0

    def acquire(self, previous: Optional[Session] = None) -> Session:
        self.refreshes += 1
        return self.bootstrapper.acquire()

    def describe(self) -> Dict[str, Any]:
        # 必须带 "provider" 键，与基类及链路其它成员保持一致的契约
        # （ChainSessionProvider.describe 会逐个读取它）。
        return {"provider": self.name, **self.bootstrapper.describe(),
                "refreshes": self.refreshes}


def bootstrapper_from_env(
    env: Optional[Mapping[str, str]] = None,
) -> Optional[AppSessionBootstrapper]:
    """从环境变量构造引导器；凭据缺失时返回 None（交由上层回退）。

    只读取环境变量，**不含任何硬编码凭据**（AGENTS.md §3.4）。
    需要 `LEYU_APP_TOKEN` + `LEYU_APP_UUID` + `LEYU_APP_SIGNATURE`。
    """
    e = env if env is not None else __import__("os").environ
    token = (e.get(APP_ENV_TOKEN) or "").strip()
    uuid = (e.get(APP_ENV_UUID) or "").strip()
    signature = (e.get(APP_ENV_SIGNATURE) or "").strip()
    if not (token and uuid and signature):
        return None
    return AppSessionBootstrapper(
        AppCredentials(token, uuid),
        signature=signature,
        app_host=(e.get(APP_ENV_HOST) or "").strip() or DEFAULT_APP_HOST,
    )
