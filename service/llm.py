#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM 客户端（T4）—— 复用 **pi 当前模型配置**，零第三方依赖。

## 配置来源（按用户要求：使用 pi 的当前模型配置信息）

按优先级读取：

1. 显式参数（`provider` / `model`）—— 供测试与临时切换
2. 环境变量 `PI_PROVIDER` / `PI_MODEL`（pi 运行时会设置；本项目与 pi 同源）
3. `~/.pi/agent/settings.json` 的 `defaultProvider` / `defaultModel`
4. `~/.pi/agent/models.json` 中该 provider 的第一个模型

凭据（api key）读取 `~/.pi/agent/auth.json` 的 `<provider>.key`；
**绝不硬编码、绝不入库**（AGENTS.md §3.4）。也支持环境变量覆盖
（`PI_LLM_API_KEY`），便于容器注入而不挂载 pi 的配置目录。

## 协议

实测该 provider 为 **OpenAI 兼容**（`baseUrl` 形如 `http://host:port/v1`，
`api: "openai-responses"`），但 `/chat/completions` 可用：

    POST {baseUrl}/chat/completions
    Authorization: Bearer <key>

响应同时可能带 `reasoning_content`（推理型模型），正文在
`choices[0].message.content`。

## 设计约束

- service 层不得直接操作 HTTP 细节 → 本模块收敛全部协议细节
- 零第三方依赖（AGENTS.md §3.2）：仅 urllib
- 结构化输出：提供 `complete_json()`，从模型输出中稳健地提取 JSON
  （模型常在 JSON 外加解释或代码围栏）
"""

from __future__ import annotations

import json
import os
import random
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

__all__ = [
    "LLMConfig",
    "LLMError",
    "LLMNotConfigured",
    "LLMClient",
    "load_pi_config",
    "extract_json",
    "_repair_truncated_json",
]

#: pi 配置目录（允许通过环境变量覆盖，便于容器挂载）
PI_AGENT_DIR_ENV = "PI_AGENT_DIR"
DEFAULT_PI_AGENT_DIR = "~/.pi/agent"

#: 可覆盖凭据的环境变量（容器里不必挂载 pi 的配置目录）
ENV_API_KEY = "PI_LLM_API_KEY"
ENV_PROVIDER = "PI_LLM_PROVIDER"
ENV_MODEL = "PI_LLM_MODEL"
ENV_BASE_URL = "PI_LLM_BASE_URL"

DEFAULT_TIMEOUT_S = 120.0
#: 默认重试次数。上游实测会间歇返回 502
#: （`Upstream service temporarily unavailable`，实测 47 次调用失败 15 次），
#: 2 次不够——重试 4 次能把绝大多数瞬时故障抹掉。
DEFAULT_MAX_RETRIES = 4
#: 温度：分析决策场景要稳定可复现，不用高随机性
DEFAULT_TEMPERATURE = 0.2
#: 默认输出预算：推理型模型需要先“思考”，预算过小会导致正文为空
#: （实测 max_tokens=120 时 content 为空、全部 token 花在 reasoning_content）
DEFAULT_MAX_TOKENS = 4096

#: 熔断阈值：连续失败多少次后暂停上报（跳过一次网络调用）。
#: 实测背景：同机 LLM 网关 `sub2api` 的 `deepseek-v4.1-flash` 上游账号池
#: 掉线时，每个请求都要经「4 次重试 + 退避」才失败（单次约 30s）。历史
#: 记录里一天攒下 **573 次失败**（成功仅 109 次）——上游已死却仍在猛敲，
#: 既白等又给同机网关加无谓压力（本机还跑着 pi 的对话链路）。
DEFAULT_FAILURE_THRESHOLD = 3
#: 熔断后的冷却窗口（秒）。窗口内直接失败；到期放一个**半开**探测请求，
#: 成功则立刻恢复，失败则重新计时（指数上限见 _COOLDOWN_MAX_S）。
DEFAULT_COOLDOWN_S = 60.0


class LLMError(Exception):
    """LLM 调用失败。"""


class LLMNotConfigured(LLMError):
    """未找到可用的模型配置或凭据（给出可操作提示）。"""


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

#: 这些占位符说明"配置存在但未填"，必须当作未配置而不是发出去。
#: 全部小写存储：比较前会把输入 `lower()`，若集合里混有大写
#: 则 `"your_api_key"` 这类小写写法会绕过校验（真实存在过的缺陷）。
_PLACEHOLDER_KEYS = frozenset(
    {s.lower() for s in
     {"", "YOUR_API_KEY", "YOUR_SPORTSDATA_API_KEY", "sk-xxx",
      "changeme", "none", "null", "undefined"}})


@dataclass(frozen=True)
class LLMConfig:
    """LLM 连接配置。`api_key` 绝不打印。"""

    base_url: str
    model: str
    provider: str = ""
    # 注意：这里的 None 表示“未提供凭据”，真实密钥只能来自配置文件或
    # 环境变量（AGENTS.md §3.4 严禁硬编码）。
    api_key: Optional[str] = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_retries: int = DEFAULT_MAX_RETRIES
    #: 熔断：连续失败 `failure_threshold` 次后冷却 `cooldown_s` 秒（0 = 关闭）
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    cooldown_s: float = DEFAULT_COOLDOWN_S
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = 2048
    source: str = ""   # 配置来源说明（排障用）

    def __post_init__(self) -> None:
        # 在**构造点**做 scheme 校验：否则直接构造 LLMConfig(...) 可绕过
        # load_pi_config 的检查，把 base_url 指向 file:// 造成本地文件读取。
        _safe_base_url(self.base_url)
        if not self.model:
            raise LLMNotConfigured("LLMConfig.model 不能为空")

    @property
    def chat_url(self) -> str:
        base = self.base_url.rstrip("/")
        # 已包含 /chat/completions 时直接用（兼容用户直接给完整端点）
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def masked(self) -> Dict[str, Any]:
        """可安全写日志/返回前端的描述。"""
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "has_key": bool(self.api_key),
            "source": self.source,
            "timeout_s": self.timeout_s,
            "failure_threshold": self.failure_threshold,
            "cooldown_s": self.cooldown_s,
        }


def _env_int(name: str, default: int) -> int:
    """读整数环境变量；缺失/非法时用默认值（不因配置笔误中断服务）。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """读浮点环境变量；缺失/非法时用默认值。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _pi_dir() -> Path:
    return Path(os.environ.get(PI_AGENT_DIR_ENV) or DEFAULT_PI_AGENT_DIR).expanduser()


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


#: 输出预算因“推理占满”时，自动扩容重试的次数与上限
TRUNCATION_RETRIES = 2
MAX_TOKEN_BUDGET = 32768

#: LLM 重试退避（实测上游会间歇 502）
LLM_BACKOFF_BASE_S = 1.5
LLM_BACKOFF_MAX_S = 8.0
#: 熔断冷却的封顶（避免长时间掉线时冷却指数增长到离谱的值）
_COOLDOWN_MAX_S = 1800.0


def _safe_base_url(url: str) -> str:
    """校验并规范化 base_url，**只允许 http/https**。

    `urllib` 会接受 `file://` 等 scheme；base_url 来自配置文件与环境变量，
    若不加限制，一个被篡改的配置就可能变成任意本地文件读取。
    与 `collector.leyu_app_session._safe_base_url` 同一约定。
    """
    from urllib.parse import urlsplit

    candidate = str(url or "").strip().rstrip("/")
    scheme = urlsplit(candidate).scheme.lower()
    if scheme not in ("http", "https"):
        raise LLMNotConfigured(
            "LLM base_url 必须是 http/https，实际为 %r" % (candidate[:80],))
    return candidate


def load_pi_config(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> LLMConfig:
    """按用户要求读取 **pi 当前模型配置**。

    Args:
        provider: 显式指定 provider（覆盖环境与 pi 设置）。
        model: 显式指定模型 id。
        env: 环境变量映射（测试用）。

    Returns:
        LLMConfig

    Raises:
        LLMNotConfigured: 找不到模型或凭据时，抛出**可操作**的错误。
    """
    e = env if env is not None else os.environ
    agent = _pi_dir()
    settings = _read_json(agent / "settings.json")
    models = _read_json(agent / "models.json")
    auth = _read_json(agent / "auth.json")

    # 1) provider
    prov = (provider or e.get(ENV_PROVIDER) or e.get("PI_PROVIDER")
            or settings.get("defaultProvider") or "").strip()
    providers = (models.get("providers") or {}) if isinstance(models, dict) else {}
    if not prov:
        # pi 只有一个自定义 provider 时可直接采用
        if len(providers) == 1:
            prov = next(iter(providers))
        else:
            raise LLMNotConfigured(
                "未找到 LLM provider 配置。请任选一种方式指定：\n"
                "  1) 设置 %s=<provider>\n"
                "  2) 在 %s/settings.json 写入 defaultProvider\n"
                "  3) 调用时显式传 provider=" % (ENV_PROVIDER, agent))

    pconf = providers.get(prov) or {}
    models_list = pconf.get("models") or []
    if not isinstance(models_list, list):
        models_list = []

    # 2) model
    mdl = (model or e.get(ENV_MODEL) or e.get("PI_MODEL")
           or settings.get("defaultModel") or "").strip()
    available = [str(m.get("id")) for m in models_list
                 if isinstance(m, Mapping) and m.get("id")]
    if mdl:
        # pi 的 defaultModel 可能是 "vendor/id" 形式，而 provider 用裸 id
        if mdl not in available:
            tail = mdl.rsplit("/", 1)[-1]
            if tail in available:
                mdl = tail
    if not mdl:
        if not available:
            raise LLMNotConfigured(
                "provider %r 在 %s/models.json 中没有可用模型" % (prov, agent))
        mdl = available[0]

    # 3) base_url
    base = (e.get(ENV_BASE_URL) or pconf.get("baseUrl") or "").strip()
    if not base:
        raise LLMNotConfigured(
            "provider %r 缺少 baseUrl（%s/models.json 的 providers.%s.baseUrl）"
            % (prov, agent, prov))

    # 4) api key
    key_env = (e.get(ENV_API_KEY) or "").strip()
    aconf = auth.get(prov) if isinstance(auth, dict) else {}
    key_file = ""
    if isinstance(aconf, Mapping):
        key_file = str(aconf.get("key") or aconf.get("apiKey") or "").strip()
    key = key_env or key_file

    if key.strip().lower() in _PLACEHOLDER_KEYS:
        raise LLMNotConfigured(
            "provider %r 的 API key 是占位符/为空，无法调用。"
            "请设置 %s 或检查 %s/auth.json" % (prov, ENV_API_KEY, agent))

    # max tokens：取模型声明的值（避免超出该模型上限）
    max_tokens = 2048
    for m in models_list:
        if isinstance(m, Mapping) and str(m.get("id")) == mdl:
            mt = m.get("maxTokens")
            if isinstance(mt, int) and mt > 0:
                # 取较小值：推理型模型输出可能很长，但单次分析不需要全量上限
                max_tokens = min(int(mt), 4096)
            break

    src = "参数" if provider or model else (
        "环境变量" if (e.get(ENV_PROVIDER) or e.get("PI_PROVIDER")) else "pi settings.json")
    return LLMConfig(
        base_url=_safe_base_url(base), model=mdl, provider=prov, api_key=key,
        max_tokens=max_tokens, source=src,
        # 熔断阈值：0 = 关闭（恢复“每次都真打”的旧行为，便于对比排查）
        failure_threshold=_env_int("ANALYSIS_LLM_FAILURE_THRESHOLD",
                                   DEFAULT_FAILURE_THRESHOLD),
        cooldown_s=_env_float("ANALYSIS_LLM_COOLDOWN_S", DEFAULT_COOLDOWN_S),
    )


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #

def extract_json(text: str) -> Optional[Any]:
    """从模型输出中稳健地提取 JSON。

    模型常把 JSON 包在解释文字或 ```json 围栏里，直接 `json.loads` 会失败。
    策略：先剥围栏，再依次尝试整段解析、找最外层 `{...}` / `[...]`、
    最后尝试**修复被截断的 JSON**（推理型模型把 token 花在思考上时很常见）。
    """
    if not text:
        return None
    s = text.strip()
    # 剥 ```json ... ``` 围栏
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if fence:
        s = fence.group(1).strip()

    candidates: List[str] = [s]
    # 最外层大括号/方括号（取最长匹配，提升嵌套对象成功率）
    for opener, closer in (("{", "}"), ("[", "]")):
        start = s.find(opener)
        end = s.rfind(closer)
        if start >= 0 and end > start:
            candidates.append(s[start:end + 1])

    for cand in candidates:
        try:
            return json.loads(cand)
        except ValueError:
            continue

    # 截断修复：补齐未闭合的字符串/括号。
    # 实测模型会输出 `{"probabilities":{...},` 然后预算耗尽，
    # 直接丢弃会让整场决策降级成 no_llm。
    repaired = _repair_truncated_json(s)
    if repaired is not None:
        try:
            return json.loads(repaired)
        except ValueError:
            pass
    # 全部候选都失败：返回 None 让调用方报错（不返回假数据）
    return None


def _repair_truncated_json(text: str) -> Optional[str]:
    """尝试把被截断的 JSON 补齐到可解析状态。

    只做**结构层补齐**（丢弃未完成的末段，并闭合括号），
    不编造缺失的键值——编造数值会直接污染决策，宁可让上层报错。

    实测的典型截断：`{"probabilities": {...}, "confid`
    此行既未闭合字符串、也未给出冒号与值；必须把整个
    `"confid` 段**丢掉**，而不是补个引号了事（那会得到 `"confid"}`，
    一个没有值的键，仍然解析失败）。
    """
    start = text.find("{")
    if start < 0:
        return None
    s = text[start:]

    # 逐字符扫描，记录“最后一个完整键值对结束”的位置
    depth_stack: List[str] = []
    in_str = False
    escaped = False
    #: 在每个键值对结束（即顶层/嵌套层级上遇到逗号）处记录的截断点
    safe_end = -1
    for i, ch in enumerate(s):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth_stack.append(ch)
        elif ch in "}]":
            if depth_stack:
                depth_stack.pop()
            # 一个容器正常闭合，此处也是安全的切点
            safe_end = i
        elif ch == "," and depth_stack:
            safe_end = i

    # 从最后一个安全切点截断，并去掉尾部的逗号
    if safe_end >= 0:
        buf = s[:safe_end].rstrip()
        while buf and buf[-1] == ",":
            buf = buf[:-1].rstrip()
    else:
        # 连一个完整键值对都没有：仅尝试闭合括号
        buf = s.rstrip()

    # 重新统计 buf 中未闭合的括号（截断后可能变化）
    stack: List[str] = []
    in_str = False
    escaped = False
    for ch in buf:
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    if in_str:
        buf += '"'
    if not stack:
        return buf if buf != s else None
    closer = {"{": "}", "[": "]"}
    return buf + "".join(closer[c] for c in reversed(stack))


class LLMClient:
    """OpenAI 兼容的 LLM 客户端（零第三方依赖）。

    用法::

        cfg = load_pi_config()
        client = LLMClient(cfg)
        out = client.complete("分析这场比赛", system="你是足球分析师")
        data = client.complete_json("...")   # 自动解析 JSON
    """

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.calls = 0
        self.failures = 0
        #: 真正发出网络的次数（含失败，不含被熔断拦下的）
        self.attempts = 0
        self.last_error = ""
        self.total_latency_s = 0.0
        # 熔断状态（见 DEFAULT_FAILURE_THRESHOLD 的注释）
        self._consecutive_failures = 0
        self._open_until = 0.0          # 单调时钟；>now 表示熔断中
        self._skipped = 0               # 被熔断拦下、未发网络的调用数
        self._half_open = False         # 本次探测是否半开

    # -- 熔断 ---------------------------------------------------------------

    def _breaker_check(self, now: float) -> Optional[str]:
        """返回阻断原因（应直接失败），None 表示放行。

        半开语义：冷却到期的**第一个**调用放行去探测上游（标记 `_half_open`），
        其余在结果出来前仍被拦，避免上游刚挂就涌进去一批。
        """
        if self.config.failure_threshold <= 0 or self._open_until <= 0.0:
            return None
        if now < self._open_until:
            return ("LLM 连续失败 %d 次，熔断至 %+.1fs 后（冷却 %gs）"
                    % (self._consecutive_failures,
                       self._open_until - now, self.config.cooldown_s))
        if self._half_open:
            return "LLM 熔断半开探测进行中，本次跳过"
        self._half_open = True   # 放行一个探测
        return None

    def _breaker_record(self, *, failed: bool) -> None:
        """按调用结果更新熔断状态。

        ⚠️ 参数用**关键字**并命名为 `failed`（而非 `ok`）。早期版本用
        位置参数 `ok: bool`，调用点却传了 `_is_upstream_failure(last)`
        （语义相反的 True=失败），结果**一旦失败就把计数清零**，熔断永远
        不触发 —— 单测 `test_opens_after_threshold_and_skips_network` 抓到。
        """
        self._half_open = False
        if not failed:
            self._consecutive_failures = 0
            self._open_until = 0.0
            return
        if self.config.failure_threshold <= 0:
            # 熔断已关闭：只计数、不建状态（否则 health 会报一个永不放行的 open）
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.config.failure_threshold:
            # 冷却时间随连续失败数递增，上限 _COOLDOWN_MAX_S：
            # 上游长时段掉线时不再每分钟敲一次。
            over = self._consecutive_failures - self.config.failure_threshold
            self._open_until = (time.monotonic()
                                + min(self.config.cooldown_s * (2 ** over),
                                      _COOLDOWN_MAX_S))

    # -- 底层 ---------------------------------------------------------------

    def _ssl_ctx(self) -> ssl.SSLContext:
        """这些自建端点常用自签证书；仅访问用户自有推理服务。"""
        ctx = ssl.create_default_context()
        if self.config.base_url.startswith("https"):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _post(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.config.chat_url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.config.api_key:
            req.add_header("Authorization", "Bearer " + self.config.api_key)
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=self.config.timeout_s,
                                    context=self._ssl_ctx()) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise LLMError("响应非 JSON: %s" % raw[:200]) from exc
        if not isinstance(data, Mapping):
            raise LLMError("响应不是对象: %s" % str(data)[:200])
        return data

    @staticmethod
    def _content_of(resp: Mapping[str, Any]) -> str:
        """取正文。

        ⚠️ 实测该 provider 是**推理型**模型：响应同时含 `content`（答案）与
        `reasoning_content`（思考过程）。当 `max_tokens` 偏小时，模型会把
        token 全花在思考上，`content` 变成空串。

        早期实现会在 content 为空时**回退返回 reasoning_content**，这等于把
        思考过程当答案（会把 JSON 解析成一堆无关文本）。现改为抛错并提供
        可操作提示：这是“上下文预算不足”，而非“模型没回答”。
        """
        # The configured gateway returns {success:true,data:ChatCompletion}.
        # Only unwrap an explicit success; error envelopes remain errors.
        wrapped = resp.get("data")
        if resp.get("success") is True and isinstance(wrapped, Mapping):
            resp = wrapped
        choices = resp.get("choices") or []
        if not isinstance(choices, Sequence) or not choices:
            raise LLMError("响应缺少 choices: %s" % str(resp)[:160])
        choice = choices[0] if isinstance(choices[0], Mapping) else {}
        msg = choice.get("message") if isinstance(choice, Mapping) else None
        if not isinstance(msg, Mapping):
            raise LLMError("响应缺少 message: %s" % str(choice)[:160])

        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content

        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        finish = str(choice.get("finish_reason") or "")
        if isinstance(reasoning, str) and reasoning.strip():
            raise LLMError(
                "模型只产生了推理内容、正文为空（finish_reason=%s，推理 %d 字）。"
                "这是 max_tokens 不足所致（推理型模型会先用 token 思考）。"
                "请增大 max_tokens（当前 %s）后重试。"
                % (finish or "?", len(reasoning), "见调用参数"))
        return ""

    @staticmethod
    def _read_error_body(exc: urllib.error.HTTPError) -> str:
        """读 HTTP 错误体（不超过 160 字）。读不出来返回空串。

        单独抽出是为了避免 `except: pass` 这种空捕获块（静态检查会拦），
        同时保留“读错误体失败不影响主流程”的语义。
        """
        try:
            return exc.read().decode("utf-8", "replace")[:160]
        except (OSError, ValueError, AttributeError):
            return ""

    # -- 公开 API -----------------------------------------------------------

    def complete(
        self,
        prompt: str,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """单轮补全，返回正文字符串。失败抛 LLMError。"""
        messages: List[Dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: Dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": (self.config.temperature if temperature is None
                            else temperature),
            "max_tokens": max_tokens or self.config.max_tokens,
        }

        last: Optional[Exception] = None
        t0 = time.time()
        # ⚠️ 熔断用**单调**时钟：`_open_until` 也是 monotonic。早期版本这里传
        # `time.time()`（epoch，~1.79e9），而 `_open_until` 是 monotonic（~小数），
        # 相减永远为负 → 熔断器**永远读作未开**。单测抓到（混合时钟域）。
        blocked = self._breaker_check(time.monotonic())
        if blocked is not None:
            # 熔断中：不发网络、不耗退避，直接失败（诚实上报，不伪造结果）
            self._skipped += 1
            self.failures += 1
            self.last_error = blocked
            raise LLMError("%s（provider=%s model=%s）"
                           % (blocked, self.config.provider, self.config.model))
        for attempt in range(max(1, self.config.max_retries)):
            try:
                self.attempts += 1
                resp = self._post(payload)
                self.calls += 1
                self.total_latency_s += time.time() - t0
                out = self._content_of(resp)
                self._breaker_record(failed=False)
                return out
            except urllib.error.HTTPError as exc:
                detail = self._read_error_body(exc)
                last = LLMError("HTTP %d: %s" % (exc.code, detail))
                # 4xx（除 429）是请求本身有问题，重试无意义
                if 400 <= exc.code < 500 and exc.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError, OSError, LLMError) as exc:
                last = exc if isinstance(exc, LLMError) else LLMError(str(exc))
            if attempt + 1 < max(1, self.config.max_retries):
                # 渐进退避：5xx/超时往往是上游瞬时不可用，稍等即可恢复。
                # 上限 8s，避免把总延迟拖得太长（决策接口是同步等待的）。
                time.sleep(min(LLM_BACKOFF_BASE_S * (2 ** attempt),
                               LLM_BACKOFF_MAX_S) + random.random() * 0.4)
        self.failures += 1
        self.last_error = str(last)
        # 仅“上游不可用”计入熔断；4xx（请求本身错）不熔断，否则一个写错的
        # prompt 会把整条链路停掉。HTTPError 的 5xx 已含在最后的 last 里。
        self._breaker_record(failed=self._is_upstream_failure(last))
        raise LLMError("LLM 调用失败（%s/%s）: %s"
                       % (self.config.provider, self.config.model, last))

    @staticmethod
    def _is_upstream_failure(last: Optional[Exception]) -> bool:
        """判断失败是否属于“上游不可用”（应该熔断）。

        4xx 是请求/凭据错误 —— 重试与熔断都救不了，不该连坐整条链路。
        """
        text = str(last or "")
        if text.startswith("HTTP 4") and not text.startswith("HTTP 429"):
            return False
        return True

    def complete_json(
        self,
        prompt: str,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Any:
        """补全并要求 JSON 输出；解析失败抛 LLMError（不返回假数据）。

        预算自适应：推理型模型会把 token 先花在思考上（实测 `finish_reason=length`
        时正文被截断）。因此当“只有推理、没有正文”时自动**加倍预算重试**，
        避免把“预算不够”误判成“调用失败”。
        """
        budget = max_tokens or self.config.max_tokens
        last: Optional[Exception] = None
        for _ in range(TRUNCATION_RETRIES + 1):
            try:
                text = self.complete(prompt, system=system, max_tokens=budget)
            except LLMError as exc:
                last = exc
                # 仅当确实是“预算耗尽导致正文为空”时才扩容重试
                if "正文为空" not in str(exc) or budget >= MAX_TOKEN_BUDGET:
                    raise
                budget = min(budget * 2, MAX_TOKEN_BUDGET)
                continue
            data = extract_json(text)
            if data is not None:
                return data
            last = LLMError("模型输出无法解析为 JSON: %s" % text[:240])
            break
        self.last_error = str(last)
        if last is not None:
            raise last
        raise LLMError("模型输出无法解析为 JSON")

    def health(self) -> Dict[str, Any]:
        now = time.monotonic()
        until = max(0.0, self._open_until - now)
        return {
            **self.config.masked(),
            # calls 只计**成功**的；attempts 计发出去的（含失败）。
            # 早期只暴露 calls，于是上游全挂时会出现 calls=0 / failures=362
            # 这种看着像 bug 的组合。
            "calls": self.calls,
            "attempts": self.attempts,
            "failures": self.failures,
            "avg_latency_s": round(self.total_latency_s / self.calls, 2)
                             if self.calls else None,
            "last_error": self.last_error,
            "breaker": {
                "open": until > 0.0,
                "consecutive_failures": self._consecutive_failures,
                "cooldown_remaining_s": round(until, 1),
                "skipped": self._skipped,
            },
        }
