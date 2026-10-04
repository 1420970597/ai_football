#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
乐鱼 H5 站点会话引导（Cookie `X-API-TOKEN` → `requestId`）—— **与浏览器操作一致**。

═══════════════════════════════════════════════════════════════════════════
为什么需要这个模块（与 `leyu_app_session.py` 的分工）
═══════════════════════════════════════════════════════════════════════════

`leyu_app_session.py` 走的是 **App 网关**（`x-api-client: sport_android`），
它要求 `x-api-token` **请求头**；而 H5 站点把同一份凭据放在 **Cookie** 里。

两者换 `requestId` 的最后一跳完全相同（`POST /game/api/v1/venue/launch`），
差别只在「凭据放在哪」与「用哪个网关」：

    | | App 引导 | H5 引导（本模块） |
    | --- | --- | --- |
    | 凭据载体 | `x-api-token` 请求头 | `Cookie: X-API-TOKEN=…` |
    | 网关 | `LEYU_APP_HOST`（App 域名） | `LEYU_H5_SITE`（网页域名，如 `https://www.<域名>:6510`） |
    | `x-api-client` | `sport_android` | `h5` |
    | 换到的 requestId | 同源，可互换 | 同源，可互换 |

**关键实测结论（2026-10-04，`1004.pcapng` + 线上 bundle 还原）**：

1. H5 站点用 `GET /api/json-cache/y-h5-main:leyu:prod:liveDomain` 取
   **当前有效网关列表**（响应体是 `AES-128-ECB` 加密的 JSON，见下）。
   该端点**只认 Cookie**：不带 cookie 时 `venue/sort` 仍返回 200，
   但带 cookie 时才代表"已登录"。

2. `venue/launch` 的 `x-api-xxx` 前缀签名是**站点级固定值**
   （实测同一值对 `x-api-client: h5` 与 `sport_android` **都有效**，
   且**不绑定 uuid/token**），因此可长期缓存复用。
   该值从 H5 bundle 的 `localStorage.uuidToBase64` 取出后 AES 解密即得，
   或直接抓一次 App/H5 的 `venue/launch` 请求复制其 `x-api-xxx`。

3. `venue/launch` 返回的 `url` 里 `token=` 即业务 API 的 `requestId`，
   `h5Url` 的主机即业务 API 的 `Origin`。

═══════════════════════════════════════════════════════════════════════════
自动续期的真实边界（与 App 引导**完全一致**，避免误解）
═══════════════════════════════════════════════════════════════════════════

    ✅ 用**仍然有效的** `X-API-TOKEN` cookie 重新 launch，换取新 `requestId`。
       业务会话（`0401013`）失效时可自愈，全程无需人工介入。
    ❌ `X-API-TOKEN` 本身过期后重新获取它。

原因（实测，非实现偷懒）：`X-API-TOKEN` 的唯一来源是账号登录
（`POST /site/api/v1/user/login`，需手机号/密码 **+ 人机验证**
`/site/api/v1/user/member/validateGeeCheckV2`，极验系 `bcaptcha.botion.com`），
上游**不提供**任何 `refresh` / `renew` / `keepalive` 令牌刷新端点
（App + H5 + PC 全量 bundle 搜索结果为 0）。
本项目**不绕过验证码**（README §6.4），因此 cookie 过期必须人工重新提供。

> 换句话说：本模块把「浏览器每次打开页面都会做的事」自动化了 ——
   带 cookie 取网关 → launch 换 requestId。这正是"与浏览器操作一致的
   cookie 自动续期"的确切含义，而不是伪造登录。

═══════════════════════════════════════════════════════════════════════════
加密细节（A 类证据：从线上 `_app-*.js` 还原，非猜测）
═══════════════════════════════════════════════════════════════════════════

    // json-cache 响应体解密（h5 bundle 内联常量）
    var A = "A-DB/(z$/?13cfae";                       // 16 字节 key
    function g(t){ return CryptoJS.AES.decrypt(t, key, {iv: key, mode: ECB, padding: Pkcs7}) }

即 **AES-128-ECB / PKCS#7**，key = iv = 该 16 字节字符串。
（复用了 `collector.leyu_client.decrypt_aes_ecb`，零第三方依赖。）

同 bundle 中另有一组 `N2841A3412APCD6F` / `AUCDTF12H41P34Y2`（CBC）用于
localStorage 加密存储，**与本链路无关**，不要混用。
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .leyu_client import API_AES_KEY, DecodeError, decrypt_aes_ecb
from .session import Session, SessionError, SessionProvider

__all__ = [
    "H5_ENV_SITE",
    "H5_ENV_COOKIE",
    "H5_ENV_UUID",
    "H5_ENV_TOKEN",
    "H5_ENV_SIGNATURE",
    "H5_ENV_VENUE",
    "LIVE_DOMAIN_CACHE_KEY",
    "H5Credentials",
    "H5SessionBootstrapper",
    "H5SessionProvider",
    "h5_bootstrapper_from_env",
    "parse_cookie",
]

# --------------------------------------------------------------------------- #
# 环境变量（集中声明，便于部署与文档一致）
# --------------------------------------------------------------------------- #

#: H5 站点基地址（如 `https://www.56izpk.vip:6510`）
H5_ENV_SITE = "LEYU_H5_SITE"
#: 整条 Cookie 串（可选；不给则由 TOKEN/UUID 拼装）
H5_ENV_COOKIE = "LEYU_H5_COOKIE"
#: `X-API-UUID`（可选，缺失时用固定占位，实测服务端不校验其与 token 的绑定）
H5_ENV_UUID = "LEYU_H5_UUID"
#: `X-API-TOKEN`（H5 登录 cookie；也可从 `LEYU_APP_TOKEN` 复用）
H5_ENV_TOKEN = "LEYU_H5_TOKEN"
#: `x-api-xxx` 前缀签名（站点级固定值）
H5_ENV_SIGNATURE = "LEYU_H5_SIGNATURE"
#: 场馆代号，默认 YBTY（乐鱼体育）
H5_ENV_VENUE = "LEYU_H5_VENUE"

#: json-cache 的 liveDomain 键（H5 前端 `r.liveDomain`）
LIVE_DOMAIN_CACHE_KEY = "y-h5-main:leyu:prod:liveDomain"

#: 请求超时（秒）
HTTP_TIMEOUT_S = 20.0

#: H5 launch 请求体（字段与 App 侧一致，实测 H5 亦接受）
_LAUNCH_BODY: Mapping[str, Any] = {
    "enName": "YBTY",
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

#: 会话有效期（秒）：launch 换来的 token 实测数小时有效，取保守值
SESSION_TTL_S = 3600.0

#: uuid 缺省占位（实测服务端不校验 uuid 与 token 的绑定关系）
_DEFAULT_UUID = "00000000-0000-4000-8000-000000000000"


# --------------------------------------------------------------------------- #
# Cookie 解析
# --------------------------------------------------------------------------- #

def parse_cookie(raw: str) -> Dict[str, str]:
    """把 Cookie 串解析为字典（同名后者覆盖前者，与浏览器一致）。"""
    jar: Dict[str, str] = {}
    for chunk in str(raw or "").split(";"):
        name, sep, value = chunk.partition("=")
        name = name.strip()
        if sep and name:
            jar[name] = value.strip()
    return jar


def _cookie_header(jar: Mapping[str, str]) -> str:
    return "; ".join("%s=%s" % (k, v) for k, v in jar.items() if v)


# --------------------------------------------------------------------------- #
# 凭据
# --------------------------------------------------------------------------- #

class H5Credentials:
    """H5 侧凭据（`X-API-TOKEN` + `X-API-UUID` + 其它 cookie）。

    绝不写日志、绝不持久化（与 `AppCredentials` 同一纪律）。
    """

    __slots__ = ("token", "uuid", "extra")

    def __init__(self, token: str, uuid: str = "",
                 extra: Optional[Mapping[str, str]] = None) -> None:
        if not str(token).strip():
            raise SessionError(
                "缺少 H5 登录 cookie `X-API-TOKEN`（%s 或 %s）"
                % (H5_ENV_COOKIE, H5_ENV_TOKEN))
        self.token = str(token).strip()
        self.uuid = str(uuid).strip() or _DEFAULT_UUID
        self.extra = {k: v for k, v in (extra or {}).items()
                      if k not in ("X-API-TOKEN", "X-API-UUID") and v}

    @classmethod
    def from_cookie(cls, raw: str) -> "H5Credentials":
        """从整条 Cookie 串构造（浏览器 `document.cookie` 的原样复制）。"""
        jar = parse_cookie(raw)
        return cls(jar.get("X-API-TOKEN", ""), jar.get("X-API-UUID", ""),
                   {k: v for k, v in jar.items()
                    if k not in ("X-API-TOKEN", "X-API-UUID")})

    def cookie_header(self) -> str:
        jar = {"X-API-UUID": self.uuid, "X-API-TOKEN": self.token}
        jar.update(self.extra)
        return _cookie_header(jar)

    def masked(self) -> str:
        return "X-API-TOKEN=%s…%s X-API-UUID=%s" % (
            self.token[:6], self.token[-4:], self.uuid)

    def __repr__(self) -> str:  # pragma: no cover - 防误打印
        return "H5Credentials(%s)" % self.masked()


# --------------------------------------------------------------------------- #
# 引导器
# --------------------------------------------------------------------------- #

class H5SessionBootstrapper:
    """用 H5 cookie 换取业务 API 的 `requestId`（即 `Session`）。

    与浏览器打开页面时的行为**逐跳一致**：

        1. GET  /api/json-cache/<LIVE_DOMAIN_CACHE_KEY>   （Cookie 鉴权）
           → AES-128-ECB 解密 → `{"url": ["https://bkapi…", …]}`
        2. POST /game/api/v1/venue/launch                 （Cookie + x-api-token + x-api-xxx）
           → `data.url` 的 `token=` 即 `requestId`；`data.h5Url` 的主机即 `Origin`

    Args:
        credentials: H5 凭据。
        signature: `x-api-xxx` 站点级前缀签名（缺失时给出可操作报错）。
        site: H5 站点基地址（承载 `/api/json-cache` 与 `/game/api`）。
        venue: 场馆代号，默认 YBTY。
        timeout: 请求超时。
    """

    def __init__(
        self,
        credentials: H5Credentials,
        signature: str = "",
        site: str = "",
        venue: str = "YBTY",
        timeout: float = HTTP_TIMEOUT_S,
    ) -> None:
        self.credentials = credentials
        self.signature = str(signature).strip()
        self.site = _safe_base_url(site) if site else ""
        self.venue = venue or "YBTY"
        self.timeout = timeout
        self.last_error = ""
        #: 最近一次从 liveDomain 拿到的业务网关候选（排障用）
        self.last_domains: List[str] = []

    # -- HTTP -------------------------------------------------------------

    def _base(self) -> str:
        if not self.site:
            self.last_error = "缺少 %s" % H5_ENV_SITE
            raise SessionError(
                "缺少 H5 站点地址（%s）。\n"
                "取值：浏览器地址栏里打开乐鱼网页版的基地址，\n"
                "例如 `https://www.56izpk.vip:6510`（含端口）。" % H5_ENV_SITE)
        return self.site

    def _open(self, req: urllib.request.Request) -> Tuple[int, bytes, str]:
        """发起请求，返回 `(status, body, content_type)`。"""
        try:
            with urllib.request.urlopen(req, timeout=self.timeout,
                                        context=_ssl_ctx()) as resp:
                return (resp.status, resp.read(),
                        str(resp.headers.get("Content-Type") or ""))
        except urllib.error.HTTPError as exc:
            raise SessionError(
                "H5 请求失败（HTTP %d）: %s" % (exc.code, req.full_url)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.last_error = str(exc)[:120]
            raise SessionError("H5 请求失败: %s" % self.last_error) from exc

    def fetch_live_domains(self) -> List[str]:
        """取业务网关候选列表（浏览器打开页面时的第一跳）。

        响应体是 `AES-128-ECB` 加密后 base64 的 JSON：
        `{"url": ["https://bkapi3.zhanlimao.com", …]}`。

        注意：网关列表**只是候选**。实测这些 CDN 域名只承载静态资源，
        业务 API 实际仍走 `venue/launch` 返回的 `h5Url` 主机；
        本方法保留完整实现以便排障与后续切换。
        """
        base = self._base()
        url = "%s/api/json-cache/%s" % (base, urllib.parse.quote(LIVE_DOMAIN_CACHE_KEY))
        req = urllib.request.Request(url, method="GET")
        req.add_header("accept", "application/json, text/plain, */*")
        req.add_header("x-api-client", "h5")
        req.add_header("referer", base + "/")
        req.add_header("user-agent", _UA)
        req.add_header("Cookie", self.credentials.cookie_header())

        status, body, ctype = self._open(req)
        text = body.decode("utf-8", "replace").strip()
        if not text or text in ('""', "null", "{}"):
            # 实测：cookie 失效时该端点返回空串（HTTP 200），而非错误码。
            self.last_error = "liveDomain 缓存为空"
            raise SessionError(
                "H5 cookie 已失效（`%s` 返回空）。\n"
                "请重新在浏览器登录乐鱼网页版，复制新的 `X-API-TOKEN` "
                "到 %s 后重试。" % (LIVE_DOMAIN_CACHE_KEY, H5_ENV_TOKEN))
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise SessionError(
                "liveDomain 响应非 JSON（Content-Type=%s）: %s"
                % (ctype, text[:120])) from exc
        if not isinstance(payload, str):
            raise SessionError("liveDomain 响应不是密文字符串: %r" % (text[:60],))
        plain = _decrypt_cache(payload)
        try:
            data = json.loads(plain)
        except ValueError as exc:
            raise SessionError(
                "liveDomain 解密结果非 JSON: %s" % plain[:120]) from exc
        urls = data.get("url") if isinstance(data, Mapping) else None
        if isinstance(urls, str):
            urls = [urls]
        if not isinstance(urls, (list, tuple)) or not urls:
            raise SessionError("liveDomain 未返回网关列表: %s" % plain[:120])
        self.last_domains = [str(u).rstrip("/") for u in urls if str(u).strip()]
        return self.last_domains

    def launch(self, venue: Optional[str] = None) -> Mapping[str, Any]:
        """启动场馆，返回 `data` 段（含 `url` / `h5Url`）。"""
        base = self._base()
        if not self.signature:
            self.last_error = "缺少 %s" % H5_ENV_SIGNATURE
            raise SessionError(
                "缺少前缀签名 `x-api-xxx`（%s）。\n"
                "该值是**站点级固定值**（实测不绑定 uuid/token，可长期缓存）。\n"
                "获取方式：抓一次 App/H5 的 `venue/launch` 请求，\n"
                "复制其 `x-api-xxx` 请求头即可（H5 与 App 用同一个值）。"
                % H5_ENV_SIGNATURE)

        body = dict(_LAUNCH_BODY)
        body["enName"] = venue or self.venue
        req = urllib.request.Request(
            base + "/game/api/v1/venue/launch",
            data=json.dumps(body).encode("utf-8"), method="POST")
        for k, v in {
            "content-type": "application/json;charset=utf-8",
            "x-api-client": "h5",
            "x-api-version": "2.0.0",
            "x-api-site": "2001",
            "x-api-language": "CHS",
            "x-api-uuid": self.credentials.uuid,
            "x-api-token": self.credentials.token,
            "x-api-currency": "CNY",
            "x-api-xxx": self.signature,
            "user-agent": _UA,
            "referer": base + "/",
            "Cookie": self.credentials.cookie_header(),
        }.items():
            req.add_header(k, v)

        _status, raw, _ctype = self._open(req)
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise SessionError(
                "场馆启动响应非 JSON: %s" % raw[:120].decode("utf-8", "replace")) from exc
        if not isinstance(payload, Mapping):
            raise SessionError("场馆启动响应不是对象: %r" % (payload,))

        code = payload.get("status_code")
        message = str(payload.get("message") or "").strip()
        data = payload.get("data") or {}
        if code not in (6000, "6000", None) or not isinstance(data, Mapping) \
                or not data:
            self.last_error = "status_code=%s message=%s" % (code, message)
            raise SessionError(
                "场馆 %s 启动未成功（%s）。\n"
                "· `token已过期` / `请重新登录` → 更新 %s（H5 登录 cookie）后重试\n"
                "· `非法请求`(6003) → 检查 %s（x-api-xxx 前缀签名）是否正确\n"
                "· 场馆维护/未开放 → 可用 %s 换其它场馆代号"
                % (body["enName"], self.last_error,
                   H5_ENV_TOKEN, H5_ENV_SIGNATURE, H5_ENV_VENUE))
        return data

    # -- 主流程 -----------------------------------------------------------

    def acquire(self) -> Session:
        """启动场馆，把返回 URL 里的 `token` 作为 `requestId`。"""
        data = self.launch()
        launch_url = str(data.get("url") or "")
        if not launch_url:
            self.last_error = "响应缺少 data.url"
            raise SessionError("场馆 %s 启动成功但未返回 url" % self.venue)

        parts = urllib.parse.urlsplit(launch_url)
        request_id = (urllib.parse.parse_qs(parts.query).get("token") or [""])[0]
        if not request_id:
            self.last_error = "url 中缺少 token 参数"
            raise SessionError(
                "场馆 %s 的 url 未携带 token（无法取得 requestId）: %s"
                % (self.venue, launch_url[:120]))

        # Origin 取 h5Url 主机（实测业务网关按此做 CORS 校验）；
        # 缺省回退到站点自身。
        h5 = str(data.get("h5Url") or launch_url)
        origin = ""
        if h5:
            hp = urllib.parse.urlsplit(h5)
            if hp.scheme and hp.netloc:
                origin = "%s://%s" % (hp.scheme, hp.netloc)
        if not origin:
            origin = self.site

        # 业务网关：`h5Url` 的 `?api=` 参数是 **AES-128-ECB 加密**的网关地址
        # （key = `OBTY20220712OBTY`，见 leyu_client.API_AES_KEY）。
        # 这是唯一可靠的取值来源 —— `app-h5.*` 只是静态页面主机，
        # 不承载业务 API（实测 404），用错网关会导致 WebSocket 无法连接。
        host = self._api_host(h5)

        return Session(
            request_id=request_id,
            cuid="",
            # 把 H5 cookie 一并带进会话：客户端会透传到每个请求，
            # 与浏览器行为一致（业务网关可能用它做路由/风控）。
            cookie=self.credentials.cookie_header(),
            host=host,
            origin=origin,
            ttl_s=SESSION_TTL_S,
            note="来自 H5 cookie 场馆启动（%s @ %s）" % (self.venue, self.site or "-"),
        )

    def _api_host(self, h5_url: str) -> str:
        """从 `h5Url` 的 `?api=` 参数解出业务网关。

        实测（2026-10-04）：`api=` 是 AES-128-ECB + PKCS#7 + Base64 的
        明文 `https://api.<轮换域名>`，密钥 `OBTY20220712OBTY`。

        ⚠️ **不能用 `parse_qs` 取值**：base64 密文里含 `+`，而
        `parse_qs` 会把 `+` 解码成空格，导致解密失败（实测踩到）。
        因此这里用 `_raw_query_value()` 原样取值。

        解密失败时**不报错**（降级为空，由上层用默认网关），
        因为网关解密属于增强信息，不应阻断会话获取。
        """
        cipher = _raw_query_value(h5_url, "api")
        if not cipher:
            return ""
        try:
            plain = decrypt_aes_ecb(cipher, API_AES_KEY).strip()
        except (DecodeError, ValueError) as exc:
            self.last_error = "api= 解密失败: %s" % str(exc)[:80]
            return ""
        if not plain.startswith("http"):
            self.last_error = "api= 解密结果非 URL: %s" % plain[:60]
            return ""
        return plain.rstrip("/")

    def describe(self) -> Dict[str, Any]:
        return {
            "bootstrapper": "h5-cookie",
            "venue": self.venue,
            "site": self.site,
            "credentials": self.credentials.masked(),
            "signature_set": bool(self.signature),
            "last_domains": list(self.last_domains),
            "last_error": self.last_error,
        }


class H5SessionProvider(SessionProvider):
    """`SessionProvider` 适配器：把 H5 引导器接入 provider 链。

    会话失效（业务码 `0401013`）时，`collector.sources.LEYUSource` 会调用
    `invalidate()` 并重新 `acquire()` —— 即**重新 launch 场馆换新 requestId**，
    这正是"与浏览器操作一致的 cookie 自动续期"。
    """

    name = "h5-cookie"

    def __init__(self, bootstrapper: H5SessionBootstrapper) -> None:
        self.bootstrapper = bootstrapper
        self.refreshes = 0

    def acquire(self, previous: Optional[Session] = None) -> Session:
        self.refreshes += 1
        return self.bootstrapper.acquire()

    def describe(self) -> Dict[str, Any]:
        return {"provider": self.name, **self.bootstrapper.describe(),
                "refreshes": self.refreshes}


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #

def h5_bootstrapper_from_env(
    env: Optional[Mapping[str, str]] = None,
) -> Optional[H5SessionBootstrapper]:
    """从环境变量构造 H5 引导器；关键项缺失时返回 None（交由上层回退）。

    只读取环境变量，**不含任何硬编码凭据**（AGENTS.md §3.4）。

    必需：`LEYU_H5_SITE` + `x-api-xxx` 签名 + cookie（或 token）。
    兼容：cookie 可用 `LEYU_H5_COOKIE` 整条给出，也可用
    `LEYU_H5_TOKEN` / `LEYU_H5_UUID` 分开给出；token 亦可回退到
    `LEYU_APP_TOKEN`（同一份账号凭据，只是放置位置不同）。
    """
    e = env if env is not None else os.environ

    def _get(*names: str) -> str:
        for n in names:
            v = (e.get(n) or "").strip()
            if v:
                return v
        return ""

    site = _get(H5_ENV_SITE)
    signature = _get(H5_ENV_SIGNATURE, "LEYU_APP_SIGNATURE")
    if not (site and signature):
        return None

    cookie = _get(H5_ENV_COOKIE)
    if cookie:
        try:
            creds = H5Credentials.from_cookie(cookie)
        except SessionError:
            return None
    else:
        token = _get(H5_ENV_TOKEN, "LEYU_APP_TOKEN")
        if not token:
            return None
        creds = H5Credentials(token, _get(H5_ENV_UUID, "LEYU_APP_UUID"))

    return H5SessionBootstrapper(
        creds, signature=signature, site=site,
        venue=_get(H5_ENV_VENUE) or "YBTY",
    )


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")

#: json-cache 响应体的 AES-128-ECB 密钥（从线上 H5 bundle 还原，A 类证据）
H5_CACHE_AES_KEY = b"A-DB/(z$/?13cfae"


def _raw_query_value(url: str, name: str) -> str:
    """从 URL 查询串里**原样**取出某个参数（不做 `+` → 空格 解码）。

    为何不用 `urllib.parse.parse_qs`：base64 密文里含 `+`，而 `parse_qs`
    按 `application/x-www-form-urlencoded` 语义把 `+` 解成空格，
    会让后续 AES 解密静默失败（本项目实测踩到）。
    """
    query = urllib.parse.urlsplit(str(url or "")).query
    for chunk in query.split("&"):
        key, sep, value = chunk.partition("=")
        if sep and urllib.parse.unquote(key) == name:
            # 只做百分号解码（`%2B` → `+`），保留裸 `+`
            return urllib.parse.unquote(value)
    return ""


def _decrypt_cache(payload: str) -> str:
    try:
        return decrypt_aes_ecb(payload, H5_CACHE_AES_KEY)
    except (DecodeError, ValueError) as exc:
        raise SessionError(
            "liveDomain 解密失败（AES-128-ECB，key=%s）: %s"
            % (H5_CACHE_AES_KEY.decode("latin-1"), exc)) from exc


def _safe_base_url(url: str) -> str:
    """校验并规范化基地址，**只允许 http/https**（防配置注入 file://）。"""
    candidate = str(url or "").strip().rstrip("/")
    scheme = urllib.parse.urlsplit(candidate).scheme.lower()
    if scheme not in ("http", "https"):
        raise SessionError(
            "H5 站点必须是 http/https URL，实际为 %r" % (candidate[:80],))
    return candidate


def _ssl_ctx() -> ssl.SSLContext:
    """宽松 TLS 上下文（这些网关使用轮换域名 + 自签/不匹配证书）。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx
