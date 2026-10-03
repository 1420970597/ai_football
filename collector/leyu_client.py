#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
乐鱼（leyu）赛事/赔率 API 客户端 —— 协议还原自 `leyu.saz` 抓包。

═══════════════════════════════════════════════════════════════════════════
数据来源（全部为 saz 中实测、只读还原，不含任何猜测字段）
═══════════════════════════════════════════════════════════════════════════

网关与鉴权（saz 中所有 `chrome:112208` 会话的公共特征）：

    GET  https://<api-host>/yewu11/v2/m/getOriginalDataPB?t=<ms>
    POST https://<api-host>/yewu11/v1/w/structureMatchBaseInfoByMidsPB?t=<ms>
    POST https://<api-host>/yewu11/v1/w/getMatchBaseInfoByOddsPB?t=<ms>
    WS   wss://<api-host>/yewuws2/push?requestId=<32位hex>

    固定请求头:
        lang: zh
        request-code: {"panda-bss-source":"2"}
        requestId: <32 位 hex，会话期常量，同时作为 WS query>
        checkId:   pc-<md5>-<cuid>-<毫秒时间戳>       # 非签名，服务端不回校验
        Origin/Referer: https://<前端域名>/

    ⚠️ 抓包证据表明：**不存在签名参数**（没有 sign/timestamp/signature），
       服务端仅依赖 requestId 的会话一致性。故本客户端不伪造任何签名。

响应封装（所有 *PB 端点统一）：

    {"code":"0000000","data":"<base64(gzip(JSON))>","msg":"成功","ts":<ms>}

    注意 `data` 是 **base64 编码的 gzip 流**，不是 protobuf。
    少数端点（getCategoryList / eventInfo / platformsCount）直接返回明文 JSON。

关键字段规范
------------

赛事列表 `getOriginalDataPB`（实测一次返回 1868 场 / 333 联赛 / 42 运动）:
    spList[].csid / csna            运动 ID / 名称
    tids_obj[].tid / tn / tlev      联赛 ID / 名称 / 层级
    matchsList[].mid                赛事 ID（主键）
    matchsList[].mhn / man          主队 / 客队名
    matchsList[].mhlu / malu        主队 / 客队 logo（相对路径）
    matchsList[].mgt                开赛时间（毫秒字符串，EPOCH）
    matchsList[].ms                 0=未开赛 1=进行中 110=已结束
    matchsList[].mst / mmp / msc    即时分钟 / 比赛阶段 / 比分序列
    matchsList[].mcid               场次编号（如 "周六019"，部分为空）
    matchsList[].betAmount          投注额（字符串小数）

盘口 `structureMatchBaseInfoByMidsPB` / `getMatchBaseInfoByOddsPB`:
    data[].hpsPns[]                 盘口定义列表
        chpid                       盘口 ID（1/2/4/17/18/19 …）
        hpn                         盘口中文名（全场独赢/全场让球/全场大小 …）
        hpt                         盘口类型 1=独赢 2=让球 5=大小
        hmm                         1=含盘口线（让球/大小），0=不含（独赢）
        hshow                       "Yes"/"No" 是否展示
        hsw                         可选盘口线（如 "1,2,3,4,5,6"）
        mct                         主盘口线
    data[].hpsData[0].hps[]         主盘口即时赔率
        chpid                       对应 hpsPns.chpid
        ctsp                        赔率变更时间戳(ms) —— 实时性判据
        hl.hv                       盘口线（"0/0.5"、"3"、"1.5"）
        hl.ol[]                     **每一项即一个投注选项**
            oid                     选项 ID
            ot                      选项类型：1/2/X、Over/Under
            ov                      赔率 × **100000** 的整数  ← 核心编码
            ov2                     马来盘水位（字符串）
            on / onb                选项名
            otd / ots               玩法/投注项排序与标识
            cds                     数据来源：N01/N02/A01/G01/L01-Bet365/L02-12Bet
    data[].hpsData[0].hpsAdd[]     附加盘口线（同结构，hlnm 为附加线数量）

赔率编码实证：南非客胜 decimal 1.07 → `ov=107000`；平局 8.50 → `ov=850000`。
因此 `decimal = ov / 100000.0`。`ov2` 为马来盘，`decimal = 1 + 1/|ov2|`（ov2<0）
或 `1 + ov2`（ov2>0）。

推送（WebSocket，见 `WebSocketClient-DBZzJqlx.js` + `index-DKYh4SHz.js`）:
    连接   : wss://<api-host>/yewuws2/push?requestId=<32hex>
    心跳   : 每 5s 发送 {"cmd":"C0","requestId":"<hex>"}
             8s 未收到任何消息则判定超时并重连
    赛事明细页订阅:
        {"cmd":"C13","mid":"<mid>","requestId":"<hex>"}
        {"cmd":"C4","uuid":"<hex>_Z01","requestId":"<hex>"}
        收到 {"cmd":"C1301","cd":{...赛事增量...}}
    全站盘口订阅（买量最高）:
        {"cmd":"C8","key":"<分组键>","list":[...],"cufm":"L"|"LM",
         "marketLevel":<int>,"esMarketLevel":<int>,"oddsType":<...>}
        cufm="L" 节流 1.5s，"LM" 节流 4s；cclose="1" 表示退订
    其它已确认指令码（来自 WsCmd 常量表）：
        C00 关闭连接(客户端主动)  C0 心跳  C01/C03/C04/C05 赛事列表
        C2/C21/C118 盘口赔率  C3 订单  C4 主动推送订阅  C5/C51 菜单
        C6 热门直播  C7 全局开关  C9 联赛状态
    服务端下行（R_ 前缀）：
        C101 赛事状态 C102 赛事事件 C103 比分 C104 盘口状态 C105 玩法状态
        C106 注单赔率 C107 视频动画 C108 财务日结 C110 玩法计数 C112 分类切换
        C118 盘口赔率 C201/C202 订单状态/计数 C301 菜单分段 C302 开赛
        C303 玩法暂停 C801 补时 C901 联赛关闭 C1301 动画页赛事增量

域名轮换（`prod.json`，见 `DomainState`）:
    `live_domains.*` / `GAB|GAS|GAY|GACOMMON.api[]` 均为
    **AES-128-ECB + PKCS7 + Base64**，密钥（Utf8 明文，直接作为 AES key）:
        OSS/域名文件:  panda1234_1234ob
        ?api= 参数:    OBTY20220712OBTY
    解密后得到真实 https 网关，故客户端必须先解析 prod.json 再选网关。

依据 AGENTS.md §3.2：本模块属 collector 层，只做「HTTP → 领域模型」，
不引入浏览器/Selenium 依赖，仅用标准库 urllib。
"""

from __future__ import annotations

import base64
import binascii
import gzip
import json
import os
import random
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "API_PREFIX_JOB",
    "API_PREFIX_WS",
    "AUTH_ERROR_CODES",
    "RATE_LIMIT_CODES",
    "DEFAULT_HOST",
    "DEFAULT_ORIGIN",
    "DEFAULT_USER_AGENT",
    "OSS_AES_KEY",
    "API_AES_KEY",
    "LEYUError",
    "TransportError",
    "DecodeError",
    "AuthError",
    "RateLimitError",
    "LEYUMatch",
    "LEYUClient",
    "OddsQuote",
    "decode_envelope",
    "decrypt_aes_ecb",
    "decimal_from_ov",
    "decimal_from_ov2",
    "decode_prod_json",
    "parse_match_list",
    "parse_odds_block",
    "ws_subscribe_odds",
    "ws_heartbeat",
]

# --------------------------------------------------------------------------- #
# 常量（全部取自 saz 实测，禁止魔法数字散落在逻辑里）
# --------------------------------------------------------------------------- #

API_PREFIX_JOB = "yewu11"          # REST 业务前缀（BUILDIN_CONFIG.API_PREFIX_JOB）
API_PREFIX_WS = "yewuws2"          # WebSocket 前缀（API_PREFIX_WBSOCKET）

WS_PATH = "/%s/push" % API_PREFIX_WS
WS_HEARTBEAT_INTERVAL_S = 5.0      # WebSocketClient heartbeatInterval = 5s
WS_HEARTBEAT_TIMEOUT_S = 8.0       # heartbeatTimeout = 8s
WS_RECONNECT_INTERVAL_S = 4.0      # reconnectInterval = 4s

DEFAULT_HOST = "https://api.vk3whcw.com"
DEFAULT_ORIGIN = "https://user-pc-new.ztczzx.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)
DEFAULT_LANG = "zh"
REQUEST_CODE = '{"panda-bss-source":"2"}'

# prod.json / ?api= 的 AES-128-ECB 密钥（Utf8 明文）
OSS_AES_KEY = b"panda1234_1234ob"
API_AES_KEY = b"OBTY20220712OBTY"

# 赔率编码基数：ov = decimal * OV_SCALE
OV_SCALE = 100_000.0

# 后端成功码（saz 中 "0000000" 与旧版 "200" 并存）
SUCCESS_CODES = frozenset({"0000000", "200", "0"})

#: 会话失效类业务码 → 可读说明（**需要重新获取会话**）
#: 实测（2026-09-30，HTTPS 直连 api.rah492x.com）：
#:   随机 requestId → code=0401013；抓包原始 requestId → code=0000000。
#: 即 requestId 是**绑定账号的会话令牌**，不是可任意生成的追踪 ID。
AUTH_ERROR_CODES: Mapping[str, str] = {
    "0401013": "账户信息已过期，请重新登录",
    "0401014": "账户未登录",
    "0401015": "账户无权限",
}

#: 限流类业务码 → 可读说明（**应退避重试，不是会话失效**）。
#: 实测：全量采集 111 批全速请求时触发 0401038。
#: 早期实现把 0401038 归入 AUTH_ERROR_CODES，导致限流被当成
#: 会话过期而触发重新登录（无意义且加重风控），已拆分。
RATE_LIMIT_CODES: Mapping[str, str] = {
    "0401038": "当前访问人数过多，请稍后再试",
    "0400429": "请求过于频繁",
}

#: 限流退避参数（实测全量采集 111 批会触发 0401038）
#: 限流时用比普通错误更多的尝试次数与更长的等待，因为上游是在主动拦我们。
RATE_LIMIT_RETRIES = 6
RATE_LIMIT_BACKOFF_BASE_S = 1.0
RATE_LIMIT_BACKOFF_MAX_S = 15.0

# 盘口类型 hpt
HPT_WINNER = 1     # 独赢（1X2 / 上半场 1X2）
HPT_HANDICAP = 2   # 让球（亚盘）
HPT_TOTAL = 5      # 大小球

# 赛事状态 ms
MS_NOT_STARTED = 0
MS_LIVE = 1
MS_FINISHED = 110

# 选项标识
OUTCOME_HOME = "home"
OUTCOME_DRAW = "draw"
OUTCOME_AWAY = "away"

# 数据来源 cds → 人类可读名（saz 实测出现过的全部值）
CDS_NAMES: Mapping[str, str] = {
    "N01": "内部盘",
    "N02": "内部盘2",
    "A01": "聚合盘",
    "G01": "聚合盘2",
    "L01-Bet365": "Bet365",
    "L02-12Bet": "12Bet",
    "FTS": "FTS",
    "PD": "PD",
    "L02": "12Bet",
    "O01": "O01",
    "S01": "S01",
    "B02": "B02",
    "C01": "C01",
}


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #

class LEYUError(Exception):
    """乐鱼采集器统一异常基类。"""


class TransportError(LEYUError):
    """网络层失败（超时 / DNS / HTTP 非 2xx）。"""


class DecodeError(LEYUError):
    """响应体解码失败（非 JSON / base64 或 gzip 损坏 / 业务 code 非成功）。"""


class AuthError(DecodeError):
    """会话失效（业务码 0401013 等）。

    乐鱼业务端点用 `requestId` 作为绑定账号的会话令牌；
    随机生成的 requestId 会让上游返回「账户信息已过期」。
    基类为 DecodeError 以兼容旧捕获写法。
    """


class RateLimitError(DecodeError):
    """上游限流（业务码 0401038 等）。

    与 AuthError 分开：限流的正确处置是**退避重试**，
    重新获取会话既无帮助，也会加重风控。
    """


# --------------------------------------------------------------------------- #
# 编解码工具
# --------------------------------------------------------------------------- #

def _pkcs7_unpad(data: bytes, block: int = 16) -> bytes:
    """去除 PKCS#7 填充（容错：填充非法时原样返回，避免脏数据中断采集）。"""
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= block and len(data) >= pad:
        return data[:-pad]
    return data


def decrypt_aes_ecb(b64_cipher: str, key: bytes = OSS_AES_KEY) -> str:
    """AES-128-ECB + PKCS7 解密 base64 密文（等价 CryptoJS.AES.decrypt(..., {mode:ECB})）。

    使用内置纯 Python 实现，保证 collector 层零第三方依赖
    （AGENTS.md §3.2 / pyproject.toml 的依赖声明）。
    """
    raw = base64.b64decode(b64_cipher)
    if len(raw) % 16 != 0:
        raise DecodeError("AES 密文长度非 16 字节对齐: %d" % len(raw))
    out = _aes_ecb_decrypt_pure(raw, key)
    text = _pkcs7_unpad(out).decode("utf-8", "replace").strip()
    # JS 侧会把结尾多余的 "/" 去掉（get_oss_decrypt_str 的收尾逻辑）
    while text.endswith("/"):
        text = text[:-1]
    return text


def _xtime(a: int) -> int:
    """GF(2^8) 上乘 2（FIPS-197 §4.2.1）。"""
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _gmul(a: int, b: int) -> int:
    """GF(2^8) 乘法（FIPS-197 §4.2.1）。"""
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        b >>= 1
        a = _xtime(a)
    return p


def _build_aes_tables() -> Tuple[List[int], List[int]]:
    """按 FIPS-197 §5.1.1 生成 S 盒与逆 S 盒。"""
    sbox: List[int] = [0] * 256
    inv: List[int] = [0] * 256
    for i in range(256):
        # GF(2^8) 乘法逆变（a != 0 时唯一的 x 使 gmul(i, x) == 1）
        c = 0 if i == 0 else next(x for x in range(1, 256) if _gmul(i, x) == 1)
        s = c
        # 仿射变换: s = c ^ rotl(c,1) ^ rotl(c,2) ^ rotl(c,3) ^ rotl(c,4) ^ 0x63
        for _ in range(4):
            c = ((c << 1) | (c >> 7)) & 0xFF
            s ^= c
        s ^= 0x63
        sbox[i] = s
        inv[s] = i
    return sbox, inv


_SBOX, _INV_SBOX = _build_aes_tables()


def _expand_key(key: bytes) -> List[int]:
    """AES 密钥扩展（FIPS-197 §5.2），返回 4*(Nr+1) 个 32bit 字的字节序列。"""
    nk = len(key) // 4
    nr = nk + 6
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (nr + 1)):
        temp = list(w[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[b] for b in temp]
            temp[0] ^= rcon
            rcon = ((rcon << 1) ^ (0x1B if rcon & 0x80 else 0)) & 0xFF
        elif nk > 6 and i % nk == 4:
            temp = [_SBOX[b] for b in temp]
        w.append([a ^ b for a, b in zip(w[i - nk], temp)])
    return [b for word in w for b in word]


def _inv_shift_rows(s: List[int]) -> List[int]:
    """InvShiftRows（FIPS-197 §5.3.2）：第 r 行循环右移 r 字节。

    状态按列主序存储：`s[r + 4*c]`。
    """
    out = [0] * 16
    for r in range(4):
        for c in range(4):
            out[r + 4 * c] = s[r + 4 * ((c - r) % 4)]
    return out


def _inv_mix_columns(s: List[int]) -> List[int]:
    """InvMixColumns（FIPS-197 §5.3.3），列内乘固定矩阵。"""
    out = list(s)
    for c in range(4):
        a0, a1, a2, a3 = s[4 * c:4 * c + 4]
        out[4 * c + 0] = _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9)
        out[4 * c + 1] = _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13)
        out[4 * c + 2] = _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11)
        out[4 * c + 3] = _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)
    return out


def _aes_ecb_decrypt_pure(block: bytes, key: bytes) -> bytes:
    """纯 Python AES-ECB 解密（等价密码逆序，FIPS-197 §5.3）。

    仅用于解密少量域名密文，不要求高性能；
    与 `cryptography` 的 AES-ECB 结果在 tests 中逐字节对齐校验。
    """
    if len(key) not in (16, 24, 32):
        raise DecodeError("AES 密钥长度非法: %d" % len(key))
    rk = _expand_key(key)
    nr = len(key) // 4 + 6
    out = bytearray()
    for off in range(0, len(block), 16):
        # 初始 AddRoundKey 使用 **最后一轮** 轮密钥（FIPS-197 图 15）
        s = [block[off + i] ^ rk[16 * nr + i] for i in range(16)]
        for rnd in range(nr - 1, -1, -1):
            s = _inv_shift_rows(s)
            s = [_INV_SBOX[b] for b in s]
            s = [s[i] ^ rk[16 * rnd + i] for i in range(16)]
            if rnd != 0:
                s = _inv_mix_columns(s)
        out.extend(s)
    return bytes(out)


def decimal_from_ov(ov: object) -> Optional[float]:
    """把 `ov` 整数编码还原为十进制含本金赔率；非法值返回 None。"""
    try:
        raw = int(str(ov))
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None
    dec = raw / OV_SCALE
    return dec if dec > 1.0 else None


def decimal_from_ov2(ov2: object) -> Optional[float]:
    """把马来盘水位 `ov2` 还原为十进制赔率（仅作校验，主口径用 ov）。"""
    try:
        v = float(str(ov2))
    except (TypeError, ValueError):
        return None
    if v == 0.0:
        return None
    dec = (1.0 + 1.0 / abs(v)) if v < 0 else (1.0 + v)
    return dec if dec > 1.0 else None


def decode_envelope(payload: Mapping[str, Any]) -> Any:
    """解开 `{code,data,...}` 封装。

    实测存在**三种** `data` 形态，必须全部兼容：

    1. `base64(gzip(JSON))` —— 绝大多数 `*PB` 端点（如 getOriginalDataPB）
    2. 明文 JSON —— 如 `category/getCategoryList` 直回数组/对象
    3. **明文标量字符串** —— 如 `getSystemTime/currentTimeMillis` 回 `"1790812193894"`
       （早期实现把任何字符串都当 base64+gzip，导致系统时间接口必报错）
    """
    code = str(payload.get("code", ""))
    if code and code not in SUCCESS_CODES:
        if code in RATE_LIMIT_CODES:
            raise RateLimitError(
                "上游限流 code=%s（%s）。应退避后重试；"
                "请降低采集速率（增大批间隔）而非重新登录。"
                % (code, RATE_LIMIT_CODES[code]))
        hint = AUTH_ERROR_CODES.get(code)
        if hint is not None:
            raise AuthError(
                "业务返回鉴权失败 code=%s（%s）。"
                "乐鱼业务端点用 requestId 作为**绑定账号的会话令牌**，"
                "随机 requestId 无效；需提供有效会话（浏览器登录后复制）。"
                % (code, hint)
            )
        raise DecodeError("业务返回失败 code=%s msg=%s" % (code, payload.get("msg")))
    data = payload.get("data")
    if not isinstance(data, str):
        return data

    raw = _try_b64decode(data)
    if raw is not None and raw[:2] == b"\x1f\x8b":
        # 有 gzip 魔数 → 必须能解开；打开失败说明是**真损坏**，不能静默降级
        try:
            inflate = gzip.decompress(raw)
        except (OSError, EOFError, zlib.error) as exc:
            raise DecodeError("data 声明为 gzip 但解压失败: %s" % exc) from exc
        text = inflate.decode("utf-8", "replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise DecodeError("gzip 解压后非 JSON: %s" % exc) from exc

    # 无 gzip 魔数 → 明文路径（标量字符串或明文 JSON）
    text = data.strip()
    if text[:1] in ("{", "["):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise DecodeError("明文 data 非 JSON: %s" % exc) from exc
    return text


def _try_b64decode(data: str) -> Optional[bytes]:
    """尝试 base64 解码；不是合法 base64 时返回 None（而非抛错）。"""
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError, TypeError):
        # 容忍缺失 padding 的变体
        try:
            pad = "=" * (-len(data) % 4)
            return base64.b64decode(data + pad, validate=False)
        except (binascii.Error, ValueError, TypeError):
            return None


def decode_prod_json(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """解密 prod.json 中的域名池，返回 {group: [真实 https 域名]}。

    组名保留原始键（live_domains / GAB / GAS / GAY / GACOMMON …），
    只把能成功解密的密文替换为明文，失败项丢弃并保留在 `_failed` 中以便告警。
    """
    out: Dict[str, Any] = {}
    failed: List[str] = []
    for key, value in raw.items():
        if key == "live_domains" and isinstance(value, Mapping):
            bucket: Dict[str, Any] = {}
            for label, cipher in value.items():
                try:
                    bucket[label] = decrypt_aes_ecb(str(cipher), OSS_AES_KEY)
                except (DecodeError, ValueError):
                    failed.append("live_domains.%s" % label)
            out["live_domains"] = bucket
        elif isinstance(value, Mapping):
            item = dict(value)
            if "api" in item and isinstance(item["api"], Iterable):
                decrypted = []
                for idx, cipher in enumerate(item["api"]):
                    try:
                        decrypted.append(decrypt_aes_ecb(str(cipher), OSS_AES_KEY))
                    except (DecodeError, ValueError):
                        failed.append("%s.api[%d]" % (key, idx))
                item["api"] = decrypted
            out[key] = item
        else:
            out[key] = value
    if failed:
        out["_failed"] = failed
    return out


# --------------------------------------------------------------------------- #
# 领域模型
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class OddsQuote:
    """单个投注选项的赔率报价（对应 `hl.ol[]` 中的一项）。"""

    oid: str
    outcome: str            # home / draw / away / over / under / other
    label: str              # 原始 on/onb，如 "主胜"、"+1"、"大 3"
    decimal: float          # ov / 100000
    line: str = ""          # 盘口线 hv，如 "0/0.5"、"3"
    malay: Optional[float] = None   # ov2 还原的十进制（校验用）
    source: str = ""        # cds
    ctsp: int = 0           # 赔率变更时间戳(ms)

    @property
    def implied(self) -> float:
        """隐含概率 1/decimal。"""
        return 1.0 / self.decimal


@dataclass(frozen=True)
class MarketQuote:
    """一个盘口在某一时刻的完整报价集合。"""

    chpid: str
    name: str               # 盘口中文名
    hpt: int                # 1 独赢 / 2 让球 / 5 大小
    hv: str                 # 盘口线（让球族为**主队让球线**，可带符号）
    quotes: Tuple[OddsQuote, ...]
    ctsp: int = 0           # 该盘口报价的变更时间戳(ms)

    @property
    def booksum(self) -> float:
        """Σ(1/decimal)，用于水位/水钱计算。"""
        return sum(q.implied for q in self.quotes)

    @property
    def margin(self) -> float:
        return self.booksum - 1.0


@dataclass(frozen=True)
class LEYUMatch:
    """赛事基础信息（来自 getOriginalDataPB / structureMatchBaseInfoByMidsPB）。"""

    mid: str
    sport_id: str
    sport: str
    tid: str
    tournament: str
    home: str
    away: str
    start_ms: int
    status: int             # ms
    minute: str = ""        # mst（即时分钟；ms==1 且 mmp=="0" 时非分钟，见 clock）
    period: str = ""        # mmp（比赛阶段）
    score_raw: str = ""     # msc 归一化串，如 "S1|2:1,S555|3:4"
    mcid: str = ""          # 场次编号（如 "周六019"）
    bet_amount: float = 0.0
    home_logo: str = ""
    away_logo: str = ""
    markets: Tuple[MarketQuote, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        return self.status == MS_LIVE

    @property
    def is_finished(self) -> bool:
        return self.status == MS_FINISHED

    @property
    def score(self) -> Tuple[Optional[int], Optional[int]]:
        """从 msc 里取 S1 的全场比分（主, 客）；缺失返回 (None, None)。"""
        for chunk in self.score_raw.split(","):
            chunk = chunk.strip().strip("'").strip()
            if not chunk.startswith("S1|"):
                continue
            _, _, val = chunk.partition("|")
            left, _, right = val.partition(":")
            try:
                return int(left), int(right)
            except ValueError:
                return None, None
        return None, None

    @property
    def clock(self) -> str:
        """可展示的比赛时钟。

        前端逻辑（`index-DKYh4SHz.js` 的 `L()`）：当 `ms==1 且 mmp=="0"` 时
        `mst` 不是分钟数（此时展示固定阶段且隐藏计时），直接展示会把
        `mst` 的原始值（如 "1712"）误当成分钟。
        """
        if self.is_live and self.period == "0":
            return "-"
        return self.minute or "-"


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #

# 选项标识 → 归一化结果名（hpt=1 独赢族）
_WINNER_OUTCOME = {"1": OUTCOME_HOME, "2": OUTCOME_AWAY, "X": OUTCOME_DRAW}
# hpt=5 大小球
_TOTAL_OUTCOME = {"Over": "over", "Under": "under"}


def _normalize_outcome(ot: str, hpt: int) -> str:
    """把后端 `ot` 映射为归一化结果名。"""
    if hpt == HPT_TOTAL:
        return _TOTAL_OUTCOME.get(ot, "other")
    if hpt == HPT_WINNER:
        return _WINNER_OUTCOME.get(ot, "other")
    # 让球族：1=主队让球方，2=客队让球方
    if ot == "1":
        return OUTCOME_HOME
    if ot == "2":
        return OUTCOME_AWAY
    return "other"


def _quote_from_entry(
    entry: Mapping[str, Any], hpt: int, hv: str, ctsp: int = 0
) -> Optional[OddsQuote]:
    """`hl.ol[]` 单项 → OddsQuote；ov 非法则丢弃该项（告警由调用方汇总）。

    Args:
        ctsp: 赔率变更时间戳（毫秒）。该字段位于**块级**（与 `hl` 同级），
            不在 `ol[]` 条目内；早先版本从条目取导致新鲜度信号恒为 0。
    """
    decimal = decimal_from_ov(entry.get("ov"))
    if decimal is None:
        return None
    ot = str(entry.get("ot", ""))
    label = str(entry.get("on") or entry.get("onb") or ot)
    return OddsQuote(
        oid=str(entry.get("oid", "")),
        outcome=_normalize_outcome(ot, hpt),
        label=label,
        decimal=decimal,
        line=hv,
        malay=decimal_from_ov2(entry.get("ov2")),
        source=str(entry.get("cds", "")),
        ctsp=ctsp,
    )


def parse_market_quote(
    hps_entry: Mapping[str, Any],
    hps_pns: Sequence[Mapping[str, Any]] = (),
    include_added: bool = False,
) -> Tuple[MarketQuote, ...]:
    """解析 `hpsData[0].hps[]`（+ 可选 hpsAdd），产出完整盘口报价。

    **每个 (chpid, 盘口线) 组合产出独立 MarketQuote**：附加盘口（hpsAdd）的同一
    个玩法会提供多条盘口线（如让球 -1 / -1.5 / -0.5），把它们合并会把
    不同线的赔率加在一起，导致 booksum / margin 失去意义。

    Args:
        hps_entry: `hpsData[i]` 字典，需含 `hps`（可选 `hpsAdd`）。
        hps_pns:   同一赛事 `hpsPns` 盘口定义，用于补全中文名与 hpt。
        include_added: 是否合并附加盘口线（hpsAdd）。
    """
    meta = {str(p.get("chpid")): p for p in hps_pns}
    blocks: List[Any] = list(hps_entry.get("hps") or ())
    if include_added:
        blocks = blocks + list(hps_entry.get("hpsAdd") or ())

    out: List[MarketQuote] = []
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        chpid = str(block.get("chpid", ""))
        info = meta.get(chpid, {})
        hpt = _to_int(info.get("hpt"))
        name = str(info.get("hpn") or block.get("hpn") or chpid)
        # 实测 `hl` 既可能是 dict（主盘口），也可能是 list（附加盘口，每项一带线）
        raw_hl = block.get("hl")
        if isinstance(raw_hl, Mapping):
            hl_list: List[Mapping[str, Any]] = [raw_hl]
        elif isinstance(raw_hl, Sequence) and not isinstance(raw_hl, str):
            hl_list = [h for h in raw_hl if isinstance(h, Mapping)]
        else:
            hl_list = []

        for hl in hl_list:
            hv = str(hl.get("hv") or "")
            quotes: List[OddsQuote] = []
            # ctsp 在块级，作用于该块内全部选项
            ctsp = _to_int(block.get("ctsp"))
            for entry in (hl.get("ol") or ()):
                if not isinstance(entry, Mapping):
                    continue
                quote = _quote_from_entry(entry, hpt, hv, ctsp)
                if quote is not None:
                    quotes.append(quote)
            if quotes:
                out.append(
                    MarketQuote(
                        chpid=chpid,
                        name=name,
                        hpt=hpt,
                        hv=hv,
                        quotes=tuple(quotes),
                        ctsp=ctsp,
                    )
                )
    return tuple(out)


def parse_odds_block(blob: Mapping[str, Any]) -> List[LEYUMatch]:
    """解析 `structureMatchBaseInfoByMidsPB` / `getMatchBaseInfoByOddsPB` 响应。

    Args:
        blob: `decode_envelope` 后的顶层对象，需含 `data`（赛事数组）。

    Returns:
        LEYUMatch 列表；无盘口的赛事同样返回（markets 为空），
        以保留赛事维度，便于上层区分「无盘口」与「未采集」。
    """
    rows = blob.get("data") if isinstance(blob, Mapping) else blob
    if not isinstance(rows, Sequence):
        raise DecodeError("盘口响应 data 非数组: %r" % type(rows).__name__)

    out: List[LEYUMatch] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        hps_pns = list(row.get("hpsPns") or ())
        markets: List[MarketQuote] = []
        for block in (row.get("hpsData") or ()):
            if isinstance(block, Mapping):
                markets.extend(parse_market_quote(block, hps_pns, include_added=True))
        out.append(
            LEYUMatch(
                mid=str(row.get("mid", "")),
                sport_id=str(row.get("csid", "")),
                sport=str(row.get("csna", "")),
                tid=str(row.get("tid", "")),
                tournament=str(row.get("tn") or row.get("tnjc") or ""),
                home=str(row.get("mhn", "")),
                away=str(row.get("man", "")),
                start_ms=_to_int(row.get("mgt")),
                status=_to_int(row.get("ms")),
                minute=str(row.get("mst", "")),
                period=str(row.get("mmp", "")),
                score_raw=_score_raw(row.get("msc")),
                mcid=str(row.get("mcid", "")),
                bet_amount=_to_float(row.get("betAmount")),
                home_logo=_first(row.get("mhlu")),
                away_logo=_first(row.get("malu")),
                markets=tuple(markets),
                raw=row,
            )
        )
    return out


def parse_match_list(blob: Mapping[str, Any]) -> List[LEYUMatch]:
    """解析 `getOriginalDataPB` —— 全量赛程（一次拿全部联赛 + 全部赛事）。"""
    if not isinstance(blob, Mapping):
        raise DecodeError("赛程响应非对象: %r" % type(blob).__name__)
    sports = {str(s.get("csid")): str(s.get("csna", "")) for s in (blob.get("spList") or ())}
    leagues = {
        str(t.get("tid")): str(t.get("tn", "")) for t in (blob.get("tids_obj") or ())
    }
    out: List[LEYUMatch] = []
    for m in (blob.get("matchsList") or ()):
        if not isinstance(m, Mapping):
            continue
        csid = str(m.get("csid", ""))
        tid = str(m.get("tid", ""))
        out.append(
            LEYUMatch(
                mid=str(m.get("mid", "")),
                sport_id=csid,
                sport=sports.get(csid, ""),
                tid=tid,
                tournament=leagues.get(tid, ""),
                home=str(m.get("mhn", "")),
                away=str(m.get("man", "")),
                start_ms=_to_int(m.get("mgt")),
                status=_to_int(m.get("ms")),
                minute=str(m.get("mst", "")),
                period=str(m.get("mmp", "")),
                score_raw=str(m.get("msc", "")),
                mcid=str(m.get("mcid", "")),
                bet_amount=_to_float(m.get("betAmount")),
                home_logo=_first(m.get("mhlu")),
                away_logo=_first(m.get("malu")),
                raw=m,
            )
        )
    return out


def _to_int(value: object, default: int = 0) -> int:
    """宽松整数转换（上游既有 int 也有 str）。"""
    try:
        return int(str(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _score_raw(value: object) -> str:
    """归一化 `msc` 为逗号分隔串。

    实测两种形态：
      * `structureMatchBaseInfoByMidsPB` → **list**，如 `["S1|2:1", …]`
      * `getOriginalDataPB` → **字段不存在**（赛程接口不返回比分）
    直接 `str()` 会把 list 变成 Python 字面量（`"['S1|2:1']"`），
    导致比分解析静默失败，所以这里显式处理。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence):
        return ",".join(str(x) for x in value)
    return ""


def _now_ms() -> int:
    """当前毫秒时间戳；取时钟失败时回退 0，绝不因时钟异常中断采集。"""
    try:
        return int(time.time() * 1000)
    except (TypeError, ValueError, OverflowError, OSError):
        return 0


def _to_float(value: object, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _first(value: object) -> str:
    if isinstance(value, Sequence) and not isinstance(value, str) and value:
        return str(value[0])
    return str(value) if isinstance(value, str) else ""


# --------------------------------------------------------------------------- #
# WebSocket 报文构造（纯函数，便于测试；实际 socket 由上层负责）
# --------------------------------------------------------------------------- #

def ws_heartbeat(request_id: str) -> str:
    """心跳报文：每 5s 一次，服务端不回则 8s 判超时（实测语义）。"""
    return json.dumps({"cmd": "C0", "requestId": request_id}, separators=(",", ":"))


def ws_subscribe_odds(
    request_id: str,
    key: str,
    mids: Sequence[str],
    cufm: str = "L",
    market_level: int = 0,
    es_market_level: int = 0,
    odds_type: Optional[str] = None,
    one_send: bool = False,
) -> str:
    """构造 C8 盘口订阅报文（全站实时赔率推送）。

    Args:
        request_id: 会话 ID，同 HTTP 的 `requestId`。
        key: 分组键；同键重复订阅会合并（服务端 WsSendManger）。
        mids: 订阅的赛事 ID 列表。
        cufm: "L"（1.5s 节流）或 "LM"（4s 节流，客户端强制单发）。
        market_level / es_market_level: 用户盘口层级。
        odds_type: 赔率类型（欧赔/亚盘切换）。
        one_send: True 时发送 LM 单发模式，请求体不参与合并。
    """
    if cufm not in ("L", "LM"):
        raise ValueError("cufm 只能是 'L' 或 'LM'，实际 %r" % cufm)
    body: Dict[str, Any] = {
        "cmd": "C8",
        "list": [{"mid": str(m)} for m in mids],
        "cufm": "LM" if one_send else cufm,
        "marketLevel": market_level,
        "esMarketLevel": es_market_level,
    }
    if not one_send:
        body["key"] = key
    if odds_type is not None:
        body["oddsType"] = odds_type
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------- #
# HTTP 客户端
# --------------------------------------------------------------------------- #

class LEYUClient:
    """乐鱼 REST 客户端（零第三方依赖，仅标准库 urllib）。

    用法::

        client = LEYUClient(request_id="a91bcf06...")   # 32 位 hex
        matches = client.all_matches()                  # 全量赛程
        detail = client.matches_by_mids(["5714088"])    # 带盘口
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        origin: str = DEFAULT_ORIGIN,
        request_id: Optional[str] = None,
        check_id_base: Optional[str] = None,
        cuid: Optional[str] = None,
        lang: str = DEFAULT_LANG,
        timeout: float = 15.0,
        user_agent: str = DEFAULT_USER_AGENT,
        retries: int = 2,
    ) -> None:
        self.host = host.rstrip("/")
        self.origin = origin
        self.request_id = request_id or os.urandom(16).hex()
        self.check_id_base = check_id_base or os.urandom(16).hex()
        self.cuid = cuid or "%d1" % _now_ms()
        self.lang = lang
        self.timeout = timeout
        self.user_agent = user_agent
        self.retries = max(1, retries)

    # -- 内部 ---------------------------------------------------------------

    def _headers(self, json_body: bool) -> Dict[str, str]:
        now_ms = _now_ms()
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "lang": self.lang,
            "request-code": REQUEST_CODE,
            "requestId": self.request_id,
            # checkId 非签名，服务端不回校验；格式与抓包一致即可
            "checkId": "pc-%s-%s-%d" % (self.check_id_base, self.cuid, now_ms),
            "Origin": self.origin,
            "Referer": self.origin + "/",
            "User-Agent": self.user_agent,
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _request(self, method: str, path: str, body: Optional[Mapping[str, Any]] = None) -> Any:
        """发起请求，带指数退避与（限流专用的）长退避。

        限流（`RateLimitError`）与网络错误的退避策略不同：
        限流说明上游在主动拦我们，需要更长的等待；继续快速重试只会
        加重风控。因此限流用独立的更长退避序列。
        """
        url = "%s/%s%s" % (self.host, API_PREFIX_JOB, path)
        url += "%st=%d" % ("&" if "?" in url else "?", _now_ms())
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        last_exc: Optional[Exception] = None
        # 限流时用更多尝试次数 + 更长退避（实测全量采集会触发 0401038）
        attempts = max(self.retries, RATE_LIMIT_RETRIES)
        for attempt in range(attempts):
            req = urllib.request.Request(url, data=data, method=method)
            for k, v in self._headers(data is not None).items():
                req.add_header(k, v)
            req.add_header("Accept-Encoding", "gzip")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                payload = json.loads(raw.decode("utf-8", "replace"))
                return decode_envelope(payload)
            except RateLimitError as exc:
                # 限流：长退避（1s→2s→4s→8s，封顶 15s）+ 抖动
                last_exc = exc
                if attempt + 1 < attempts:
                    wait = min(RATE_LIMIT_BACKOFF_BASE_S * (2 ** attempt),
                               RATE_LIMIT_BACKOFF_MAX_S)
                    time.sleep(wait + random.random() * 0.5)
                continue
            except urllib.error.HTTPError as exc:
                last_exc = TransportError("HTTP %d: %s" % (exc.code, url))
                if exc.code < 500:
                    raise last_exc from exc
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                last_exc = TransportError("请求失败 %s: %s" % (url, exc))
            if attempt + 1 < attempts:
                # 指数退避 + 抖动，避免触发风控
                time.sleep(0.5 * (2 ** attempt) + random.random() * 0.3)
        raise last_exc or TransportError("请求失败: %s" % url)

    # -- 公开 API -----------------------------------------------------------

    def all_matches(self) -> List[LEYUMatch]:
        """全量赛程（含未开赛 / 进行中 / 已结束），一次覆盖全部运动与联赛。"""
        blob = self._request("GET", "/v2/m/getOriginalDataPB")
        return parse_match_list(blob)

    def matches_by_mids(self, mids: Sequence[str]) -> List[LEYUMatch]:
        """批量拉取指定赛事的**完整盘口**（含 hpsAdd 附加盘口线）。"""
        if not mids:
            return []
        blob = self._request(
            "POST",
            "/v1/w/structureMatchBaseInfoByMidsPB",
            {
                "mids": ",".join(str(m) for m in mids),
                "cuid": self.cuid,
                "cos": 0,
                "orpt": 0,
                "euid": "3020101",
            },
        )
        return parse_odds_block(blob)

    def match_odds(self, mid: str, new_user: int = 0) -> List[LEYUMatch]:
        """单场盘口快照（赛事详情页首屏口径）。"""
        blob = self._request(
            "POST",
            "/v1/w/getMatchBaseInfoByOddsPB",
            {
                "cuid": self.cuid,
                "cos": 0,
                "orpt": 0,
                "euid": "3020101",
                "mid": str(mid),
                "mcid": 0,
                "newUser": new_user,
            },
        )
        return parse_odds_block(blob)

    def server_time_ms(self) -> int:
        """服务端时间（毫秒），用于校准本地时钟偏移。"""
        blob = self._request("GET", "/v1/getSystemTime/currentTimeMillis")
        if isinstance(blob, Mapping):
            return _to_int(blob.get("data") or blob.get("ts"))
        return _to_int(blob)

    def ws_url(self) -> str:
        """WebSocket 推送地址（wss://host/yewuws2/push?requestId=...）。"""
        scheme = "wss" if self.host.startswith("https") else "ws"
        host = self.host.split("://", 1)[-1]
        return "%s://%s%s?requestId=%s" % (scheme, host, WS_PATH, self.request_id)

    def iter_odds_batches(
        self, mids: Sequence[str], batch_size: int = 20
    ) -> Iterator[List[LEYUMatch]]:
        """按批拉取盘口，避免单次请求过大（抓包中单批为 12 场）。"""
        chunk: List[str] = []
        for mid in mids:
            chunk.append(str(mid))
            if len(chunk) >= batch_size:
                yield self.matches_by_mids(chunk)
                chunk = []
        if chunk:
            yield self.matches_by_mids(chunk)
