#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
会话生命周期管理（AGENTS.md §3.2：采集层数据源无关）。

## 背景：乐鱼会话到底怎么工作（`leyu.saz` + 前端 JS 实测还原）

乐鱼有**两套互相独立的系统**，会话机制完全不同：

| | 业务 API（`api.*`） | 站点（`www.*:3353`，Next.js） |
| --- | --- | --- |
| 会话载体 | **`requestId` 请求头** | **`X-API-TOKEN`**（localStorage + Cookie） |
| 取值来源 | `loadSharedParams(OBTY).requestId` | `cookie X-API-TOKEN` → URL `?token=` → localStorage |
| 续期 | 运行时校验后重取 | `loginInfo.isChecked` → Cookie `expires=365` 天 |
| 失效信号 | `code=0401013` 账户信息已过期 | `10004/10104/10105/6001` → 触发登出 |

关键证据（`040.js`）：

```js
// 业务 API 的会话头：从持久化 shared params 取
mergeRequestSharedHeader = (url, headers) => {
  if (isOBTY(url)) headers = {"Content-Type":"application/json",
                              requestId: loadSharedParams(OBTY).requestId};
  if (isFBTY(url)) headers = {...headers, Authorization: loadSharedParams(FBTY).Authorization};
};

// 站点 token 的取值链（Cookie → URL 参数 → localStorage）
function token() {
  return getCookie("X-API-TOKEN") || urlToken || localStorage.getItem("X-API-TOKEN");
}
// 续期：勾选「记住我」则 365 天
function persist(tok) {
  let days; if (loginInfo?.isChecked) days = 365;
  setCookie("X-API-TOKEN", tok, {path:"/", expires:days});
  localStorage.setItem("X-API-TOKEN", tok);
}
// 失效即登出
["6001","10105","10004","10104"].forEach(c => emitter.on(`${name}/code/${c}`, logout));
```

**换票端点**：`GET /yewu12/api/user/getUserInfo?token=<X-API-TOKEN>`
→ 返回 `data.userId`，前端把它存为 `cuid`。
实测该端点对无效 token 返回 **HTTP 401**，业务端点返回 `0401013`。

## 本模块的职责边界（重要）

本模块**只管理会话的生命周期**，不实现登录、不生成凭证、不绕过鉴权：

```
SessionProvider.acquire()  ──> Session(request_id, cuid, ...)
        ↑                              │
        │ 失效时重新 acquire            ↓
   （来自运维注入：环境变量 / 文件 / 登录命令）      LEYUClient 发起请求
```

会话**必须由运维提供**，来源有三条（按优先级）：

1. `CommandSessionProvider` —— 执行运维提供的登录命令，取其 stdout 的会话 JSON。
   这是实现「自动续期」的正规做法：命令内部用什么方式登录由运维决定，
   **本仓库不保存任何账号口令**。
2. `FileSessionProvider`   —— 读取会话 JSON 文件（可由外部流程定期刷新）。
3. `EnvSessionProvider`    —— 直接从环境变量读（适合手工临时使用）。

若三者都不可用，`NullSessionProvider` 会给出**可操作的**报错，
而不是静默失败或伪造凭证。
"""

from __future__ import annotations

import json
import os
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

__all__ = [
    "DEFAULT_SESSION_TTL_S",
    "Session",
    "SessionError",
    "SessionProvider",
    "NullSessionProvider",
    "EnvSessionProvider",
    "FileSessionProvider",
    "CommandSessionProvider",
    "ChainSessionProvider",
    "CachedSessionProvider",
    "make_session_provider",
    "SESSION_ENV_REQUEST_ID",
    "SESSION_ENV_CUID",
    "SESSION_ENV_COOKIE",
    "SESSION_ENV_FILE",
    "SESSION_ENV_COMMAND",
    "SESSION_ENV_APP_TOKEN",
    "SESSION_ENV_APP_UUID",
    "SESSION_ENV_APP_SIGNATURE",
]

#: 环境变量名（集中声明，便于部署与文档一致）
SESSION_ENV_REQUEST_ID = "LEYU_REQUEST_ID"
SESSION_ENV_CUID = "LEYU_CUID"
SESSION_ENV_COOKIE = "LEYU_COOKIE"
SESSION_ENV_FILE = "LEYU_SESSION_FILE"
SESSION_ENV_COMMAND = "LEYU_LOGIN_COMMAND"

#: App 引导所需凭据（与 collector.leyu_app_session 同名；此处转发以便统一文档）
SESSION_ENV_APP_TOKEN = "LEYU_APP_TOKEN"
SESSION_ENV_APP_UUID = "LEYU_APP_UUID"
SESSION_ENV_APP_SIGNATURE = "LEYU_APP_SIGNATURE"

#: 会话缓存文件：App token 过期时，已换到的业务 requestId 仍可能有效，
#: 存盘可避免“App 凭据一失效就全停采集”这种脆弱行为。
SESSION_ENV_CACHE = "LEYU_SESSION_CACHE"

#: 默认会话存活时间（秒）。实测：抓包会话约 1.5~2 小时后失效。
#: 该值仅用于**提前刷新**的启发式判断，真正的失效以 `0401013` 为准。
DEFAULT_SESSION_TTL_S = 1800.0

#: 登录命令的超时（秒）
LOGIN_COMMAND_TIMEOUT_S = 120.0


class SessionError(Exception):
    """会话不可用（缺失 / 过期 / 登录失败）。"""


# --------------------------------------------------------------------------- #
# 会话值对象
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Session:
    """一次可用的乐鱼会话。

    Attributes:
        request_id: 业务 API 的会话令牌（**必填**），写入 `requestId` 请求头。
        cuid:      用户标识，写入请求体（部分端点要求）。
        cookie:    可选 Cookie 串（站点侧 `X-API-TOKEN` 等）；业务 API 不依赖它。
        host:      该会话绑定的网关；为空表示使用客户端默认网关。
        origin:    前端 Origin（服务端按此做 CORS 校验）。
        obtained_at: 取得时间（UTC）。
        ttl_s:     预期存活秒数；到期后 provider 会主动尝试续期。
        note:      取得方式说明（排障用，不含敏感值）。
    """

    request_id: str
    cuid: str = ""
    cookie: str = ""
    host: str = ""
    origin: str = ""
    obtained_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ttl_s: float = DEFAULT_SESSION_TTL_S
    note: str = ""

    def __post_init__(self) -> None:
        # 会话凭证有两种合法形式，**至少要有一种**：
        #   * requestId —— 业务 API 的会话令牌（主力）
        #   * cookie    —— 站点侧凭证（`X-API-TOKEN`）+ nginx 粘性会话 `route`
        # 早期强制要求 requestId，导致“只给 cookie”无法建会话，
        # 而实际上服务端也会用 cookie 鉴权/路由。
        if not str(self.request_id).strip() and not str(self.cookie).strip():
            raise SessionError(
                "Session 需要 request_id 或 cookie 至少之一（两者都为空）")

    @property
    def age_s(self) -> float:
        """已存活秒数。"""
        return (datetime.now(timezone.utc) - self.obtained_at).total_seconds()

    @property
    def expired(self) -> bool:
        """是否已超过预期存活时间（启发式，真正的失效以业务码为准）。"""
        return self.ttl_s > 0 and self.age_s >= self.ttl_s

    def masked(self) -> str:
        """脱敏描述，可安全写日志。"""
        rid = self.request_id
        shown = "%s…%s" % (rid[:8], rid[-4:]) if len(rid) > 14 else "***"
        return "requestId=%s cuid=%s age=%.0fs ttl=%.0fs" % (
            shown, self.cuid or "-", self.age_s, self.ttl_s)

    def as_dict(self, include_secrets: bool = False) -> Dict[str, Any]:
        """序列化。默认**脱敏**，避免误把凭证写进日志或提交。"""
        out: Dict[str, Any] = {
            "cuid": self.cuid,
            "host": self.host,
            "origin": self.origin,
            "obtained_at": self.obtained_at.isoformat(),
            "ttl_s": self.ttl_s,
            "note": self.note,
        }
        out["request_id"] = self.request_id if include_secrets else "***"
        out["cookie"] = self.cookie if include_secrets else ("***" if self.cookie else "")
        return out


# --------------------------------------------------------------------------- #
# Provider 协议
# --------------------------------------------------------------------------- #

class SessionProvider(ABC):
    """会话提供者：负责取得与续期会话。"""

    name: str = "unknown"

    @abstractmethod
    def acquire(self, previous: Optional[Session] = None) -> Session:
        """取得一个会话。

        Args:
            previous: 上一个（通常已失效的）会话，可用于复用未变字段。
        Raises:
            SessionError: 无法取得会话。
        """

    def invalidate(self, session: Optional[Session]) -> None:  # noqa: B027
        """告知 provider 该会话已失效（默认无操作，供缓存型实现清理）。

        刻意不加 @abstractmethod：这是**可选**钩子，多数 provider（Env/File/Command…）
        无缓存可清，强制实现只会逼出一堆空方法。空实现是设计意图，非漏写。
        """

    def describe(self) -> Dict[str, Any]:
        """脱敏元信息，供 /health 与排障。"""
        return {"provider": self.name, "kind": type(self).__name__}


# --------------------------------------------------------------------------- #
# 实现
# --------------------------------------------------------------------------- #

class CachedSessionProvider(SessionProvider):
    """把成功取得的会话写入磁盘，并在主来源失败时回退到缓存。

    背景（真实故障）：App token 会过期（上游返回 `6001 token已过期`），
    但**已换到的业务 requestId 仍可能有效**。若不做缓存回退，
    App 凭据一失效就会导致实时推送与采集**全面停止**，
    而实际上业务会话还能用很久。

    行为：
    * `acquire()` 先试 `inner`；成功则落盘（原子写）。
    * `inner` 失败时，读缓存；若缓存可用则返回（并标注 note）。
    * 两者都失败才抛错。
    """

    name = "cached"

    def __init__(self, inner: SessionProvider, path: str | Path) -> None:
        self.inner = inner
        self.path = Path(path)
        #: 最近一次落盘失败的原因（供 describe/排障）
        self.last_save_error = ""

    def acquire(self, previous: Optional[Session] = None) -> Session:
        inner_err: Optional[Exception] = None
        try:
            sess = self.inner.acquire(previous)
        except SessionError as exc:
            inner_err = exc
        else:
            self._save(sess)
            return sess

        cached = self._load()
        if cached is not None:
            return cached
        raise SessionError(
            "主会话来源失败，且**尚无可用缓存**：%s\n"
            "—— 怎么办 ——\n"
            "  1) 更新 %s（App 凭据会过期，上游返回 6001 时要重取）\n"
            "  2) 或注入 LEYU_REQUEST_ID / LEYU_SESSION_FILE（手工会话）\n"
            "  3) 或提供 LEYU_LOGIN_COMMAND（登录脚本）\n"
            "  缓存文件：%s（首次成功后会写入，之后即使 App token 过期\n"
            "  也能继续采集，直到业务会话本身失效）"
            % (inner_err, SESSION_ENV_APP_TOKEN, self.path))

    def invalidate(self, session: Optional[Session]) -> None:
        # 显式失效时才删缓存：避免服务端临时抽风就丢了一个可用会话
        self.inner.invalidate(session)
        try:
            if self.path.exists():
                self.path.unlink()
        except OSError:
            pass

    def _save(self, sess: Session) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(self.path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(sess.as_dict(include_secrets=True), fh,
                          ensure_ascii=False)
            os.replace(tmp, self.path)
        except (OSError, ValueError) as exc:
            # 缓存失败不影响本次使用
            self.last_save_error = str(exc)[:120]

    def _load(self) -> Optional[Session]:
        if not self.path.exists():
            return None
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, Mapping):
                return None
            return _session_from_mapping(data, note="来自会话缓存")
        except (OSError, ValueError, SessionError):
            return None

    def describe(self) -> Dict[str, Any]:
        return {"provider": self.name, "inner": self.inner.describe(),
                "cache": str(self.path), "cache_exists": self.path.exists(),
                "last_save_error": self.last_save_error}


class NullSessionProvider(SessionProvider):
    """无会话可用：给出可操作的报错，绝不伪造凭证。"""

    name = "none"

    def __init__(self, reason: str = "") -> None:
        self.reason = reason

    def acquire(self, previous: Optional[Session] = None) -> Session:
        raise SessionError(
            "无可用的乐鱼会话（%s）。"
            "乐鱼业务 API 需要**已认证会话**产生的令牌；本系统不会伪造凭证。\n"
            "请任选一种方式注入会话后重试：\n"
            "  0) %s + %s + %s"
            " —— App 凭据启动 YBTY 场馆换取 requestId（推荐，可自动续期）\n"
            "  1) %s=\"<登录脚本>\"   —— 脚本向 stdout 输出会话 JSON\n"
            "  2) %s=/path/session.json —— 外部流程定期刷新的会话文件\n"
            "  3) %s=<requestId> [%s=<cuid>] —— 手工临时使用\n"
            "会话 JSON 字段：{\"request_id\": \"...\", \"cuid\": \"...\", "
            "\"host\": \"https://api.<gateway>\", \"origin\": \"https://<site>\"}"
            % (self.reason or "未配置",
               SESSION_ENV_APP_TOKEN, SESSION_ENV_APP_UUID, SESSION_ENV_APP_SIGNATURE,
               SESSION_ENV_COMMAND, SESSION_ENV_FILE,
               SESSION_ENV_REQUEST_ID, SESSION_ENV_CUID)
        )


def _split_argv(command: str) -> List[str]:
    """把命令字符串按 shell 风格分词，但**不经过 shell**。

    支持引号与转义；因为不交给 shell，`;` `|` `&&` `$()` 等只会变成普通
    参数（而不是可执行的第二个命令），从根上避免命令注入。
    解析失败时回退为按空白切分。
    """
    import shlex
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _session_from_mapping(raw: Mapping[str, Any], note: str) -> Session:
    """把 JSON/字典转成 Session（兼容 camelCase 与下划线命名）。"""
    def pick(*keys: str) -> str:
        for k in keys:
            v = raw.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
        return ""

    rid = pick("request_id", "requestId", "token")
    if not rid:
        raise SessionError("会话数据缺少 request_id/requestId/token 字段")
    ttl = raw.get("ttl_s", raw.get("ttl"))
    try:
        ttl_s = float(ttl) if ttl is not None else DEFAULT_SESSION_TTL_S
    except (TypeError, ValueError):
        ttl_s = DEFAULT_SESSION_TTL_S
    return Session(
        request_id=rid,
        cuid=pick("cuid", "userId", "user_id"),
        cookie=pick("cookie", "cookies"),
        host=pick("host", "api_host"),
        origin=pick("origin"),
        ttl_s=ttl_s,
        note=note,
    )


class EnvSessionProvider(SessionProvider):
    """从环境变量读会话（适合手工临时使用）。

    只读取 `LEYU_REQUEST_ID` / `LEYU_CUID` / `LEYU_COOKIE`，
    **不接受硬编码默认值**——避免凭证进仓库（AGENTS.md §3.4）。
    """

    name = "env"

    def __init__(self, env: Optional[Mapping[str, str]] = None) -> None:
        self._env = env if env is not None else os.environ

    def acquire(self, previous: Optional[Session] = None) -> Session:
        rid = (self._env.get(SESSION_ENV_REQUEST_ID) or "").strip()
        cookie = (self._env.get(SESSION_ENV_COOKIE) or "").strip()
        if not rid and not cookie:
            raise SessionError(
                "环境变量 %s 与 %s 都未设置；无法从环境取得会话"
                % (SESSION_ENV_REQUEST_ID, SESSION_ENV_COOKIE))
        return Session(
            request_id=rid,
            cuid=(self._env.get(SESSION_ENV_CUID) or "").strip(),
            cookie=cookie,
            note="来自环境变量",
        )


class FileSessionProvider(SessionProvider):
    """从 JSON 文件读会话（外部流程可定期刷新该文件）。"""

    name = "file"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def acquire(self, previous: Optional[Session] = None) -> Session:
        if not self.path.exists():
            raise SessionError("会话文件不存在: %s" % self.path)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SessionError("会话文件无法解析 %s: %s" % (self.path, exc)) from exc
        if not isinstance(data, Mapping):
            raise SessionError("会话文件应为 JSON 对象: %s" % self.path)
        return _session_from_mapping(data, note="来自文件 %s" % self.path)

    def describe(self) -> Dict[str, Any]:
        return {"provider": self.name, "path": str(self.path),
                "exists": self.path.exists()}


class CommandSessionProvider(SessionProvider):
    """执行外部登录命令取得会话（**自动续期的正规实现**）。

    命令必须向 **stdout** 输出一个 JSON 对象（字段见 `_session_from_mapping`）。
    登录方式、账号口令的管理全在命令侧，本仓库不保存任何凭据。
    命令首次输出即为 `acquire`；会话失效后会**再次执行**，从而实现续期。

    配置：环境变量 `LEYU_LOGIN_COMMAND` 或构造参数 `argv`。

    安全（CWE-78）：**不使用 shell**。`argv` 列表直接 `execve`，
    shell 元字符（`;` `|` `$()` 等）不会被解释，因此即使配置值被篡改
    也无法注入额外命令。需要管道/重定向等 shell 特性时，
    请把逻辑写进一个脚本文件，然后配置为 `["bash", "/path/login.sh"]` ——
    这比把复合命令塞进配置字符串更安全也更好维护。
    """

    name = "command"

    def __init__(
        self,
        argv: Optional[Sequence[str]] = None,
        command: Optional[str] = None,
        timeout: float = LOGIN_COMMAND_TIMEOUT_S,
        cwd: Optional[str] = None,
    ) -> None:
        resolved: List[str]
        if argv is not None:
            resolved = [str(a) for a in argv if str(a).strip()]
        elif command is not None and str(command).strip():
            # 兼容字符串形式：仅做**无 shell** 的简单分词（引号感知）
            resolved = _split_argv(str(command))
        else:
            raise SessionError("登录命令不能为空")
        if not resolved:
            raise SessionError("登录命令不能为空")
        self.argv = resolved
        self.timeout = timeout
        self.cwd = cwd
        self._last_error = ""

    @property
    def command(self) -> str:
        """命令的可读形式（供日志与 describe）。"""
        return " ".join(self.argv)

    def acquire(self, previous: Optional[Session] = None) -> Session:
        try:
            proc = subprocess.run(  # noqa: S603 - 已显式禁用 shell，argv 为列表
                self.argv, shell=False, capture_output=True,
                timeout=self.timeout, cwd=self.cwd, text=True,
            )
        except subprocess.TimeoutExpired as exc:
            self._last_error = "登录命令超时(%.0fs)" % self.timeout
            raise SessionError(self._last_error) from exc
        except OSError as exc:
            self._last_error = "登录命令无法执行: %s" % exc
            raise SessionError(self._last_error) from exc

        if proc.returncode != 0:
            err = (proc.stderr or "").strip().replace("\n", " ")[:200]
            self._last_error = "登录命令退出码 %d: %s" % (proc.returncode, err)
            raise SessionError(self._last_error)

        stdout = (proc.stdout or "").strip()
        if not stdout:
            self._last_error = "登录命令未输出内容到 stdout"
            raise SessionError(self._last_error)
        # 容忍命令在 JSON 前后打印日志：取最后一个 JSON 对象
        candidate = stdout
        if not candidate.startswith("{"):
            start = candidate.rfind("{")
            if start < 0:
                self._last_error = "登录命令 stdout 中找不到 JSON 对象"
                raise SessionError(self._last_error)
            candidate = candidate[start:]
        try:
            data = json.loads(candidate)
        except ValueError as exc:
            self._last_error = "登录命令 stdout 不是合法 JSON: %s" % exc
            raise SessionError(self._last_error) from exc
        if not isinstance(data, Mapping):
            self._last_error = "登录命令 stdout 的 JSON 不是对象"
            raise SessionError(self._last_error)
        return _session_from_mapping(data, note="来自登录命令")

    def describe(self) -> Dict[str, Any]:
        return {"provider": self.name, "command": self.command,
                "argv": list(self.argv),
                "last_error": self._last_error}


class ChainSessionProvider(SessionProvider):
    """按顺序尝试多个 provider，返回第一个成功的；全部失败则汇总报错。"""

    name = "chain"

    def __init__(self, providers: Sequence[SessionProvider]) -> None:
        self.providers = [p for p in providers if p is not None]

    def acquire(self, previous: Optional[Session] = None) -> Session:
        errors = []
        for p in self.providers:
            try:
                return p.acquire(previous)
            except SessionError as exc:
                errors.append("%s: %s" % (p.name, str(exc).splitlines()[0]))
        raise SessionError("全部会话来源均不可用：%s" % " | ".join(errors))

    def invalidate(self, session: Optional[Session]) -> None:
        for p in self.providers:
            p.invalidate(session)

    def describe(self) -> Dict[str, Any]:
        return {"provider": self.name,
                "members": [p.describe() for p in self.providers]}


def make_session_provider(
    env: Optional[Mapping[str, str]] = None,
    command: Optional[str] = None,
    session_file: Optional[str] = None,
    request_id: Optional[str] = None,
    include_app: bool = True,
) -> SessionProvider:
    """按「H5 cookie → App 场馆启动 → 命令 → 文件 → 环境变量」优先级构造 provider 链。

    显式参数优先于环境变量。都没有时返回 `NullSessionProvider`
    （使用时给出可操作报错，而不是启动即崩）。

    H5 cookie 引导（`LEYU_H5_SITE` + cookie + `x-api-xxx`）放在链首：
    它与**浏览器打开页面时的行为逐跳一致**（带 cookie 取网关 →
    `/game/api/v1/venue/launch` 换 requestId），会话失效时可自动重取。

    App 引导（`LEYU_APP_TOKEN` + `LEYU_APP_UUID` + `LEYU_APP_SIGNATURE`）
    紧随其后，作为 H5 未配置时的等价替代（同一份凭据，只是放置位置不同）。

    为避免循环导入，两者都在函数内延迟导入。
    """
    e = env if env is not None else os.environ
    cmd = (command or e.get(SESSION_ENV_COMMAND) or "").strip()
    f = (session_file or e.get(SESSION_ENV_FILE) or "").strip()
    rid = (request_id or e.get(SESSION_ENV_REQUEST_ID) or "").strip()

    providers: list[SessionProvider] = []
    if include_app:
        # H5 cookie 引导排在链首：它是**与浏览器操作一致**的取会话方式
        # （带 cookie 取网关 → launch 换 requestId），且只依赖运维注入的
        # 登录 cookie，不需要任何人工登录步骤。
        from .leyu_h5_session import H5SessionProvider, h5_bootstrapper_from_env

        h5 = h5_bootstrapper_from_env(e)
        if h5 is not None:
            providers.append(H5SessionProvider(h5))

        from .leyu_app_session import AppSessionProvider, bootstrapper_from_env

        boot = bootstrapper_from_env(e)
        if boot is not None:
            providers.append(AppSessionProvider(boot))

        # **登录续期**（用户要求：实在无法自动续期则用登录接口刷新）。
        #
        # 它排在最后：前两者只要凭据未过期就能成功，且开销更小；
        # 它们都失效时（`6001 token已过期`）才走到登录重取 token。
        # 本模块**内部**包含“登录 → launch”两步，返回可直接用的 Session。
        #
        # 上游业务拒绝会暂停账号重试，网络失败则冷却后重试。
        from .leyu_app_login import login_provider_from_env

        token_cache = ""
        cache_dir = (e.get(SESSION_ENV_CACHE) or "").strip()
        if cache_dir:
            # 与 _session.json 同级，随 output 卷持久化
            token_cache = os.path.join(
                os.path.dirname(cache_dir) or ".", "_app_token.json")
        login = login_provider_from_env(e, token_cache_path=token_cache or None)
        if login is not None:
            providers.append(login)
    if cmd:
        providers.append(CommandSessionProvider(cmd))
    if f:
        providers.append(FileSessionProvider(f))
    if rid:
        # 必须把**已解析**的 rid 传进去：显式参数应覆盖环境变量里的同名项，
        # 否则 EnvSessionProvider 会回头去读环境，使显式参数失效。
        merged = dict(e)
        merged[SESSION_ENV_REQUEST_ID] = rid
        providers.append(EnvSessionProvider(merged))
    chain: SessionProvider
    if not providers:
        reason = "未配置任何会话来源"
        if not include_app:
            reason = "未配置会话来源（且已禁用 App 引导）"
        # ⚠️ 这里**不能**直接返回 NullSessionProvider：
        # App 凭据过期/被移除时，已换到的业务 requestId 仍可能有效，
        # 必须让下面的会话缓存有机会回退（否则就是“App token 一失效全停采集”）。
        chain = NullSessionProvider(reason)
    else:
        chain = (providers[0] if len(providers) == 1
                 else ChainSessionProvider(providers))

    # 包一层会话缓存：主来源失败时回退到上次成功的会话。
    cache = (e.get(SESSION_ENV_CACHE) or "").strip()
    if cache:
        return CachedSessionProvider(chain, cache)
    return chain
