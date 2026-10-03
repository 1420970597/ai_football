#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
乐鱼（leyu）WebSocket 实时推送客户端 —— 零第三方依赖的 RFC6455 最小实现。

协议还原自 `leyu.saz` 与前端 JS（见 `docs/architecture/leyu-api-protocol.md` §5）：

    连接   : wss://<api-host>/yewuws2/push?requestId=<32hex>
    握手   : 仅需 requestId，抓包实测服务端返回 101，无 Cookie / Token 校验
    心跳   : 每 5s 发送 {"cmd":"C0","requestId":"<hex>"}；8s 无任何下行则判超时
    重连   : 4s 后重试
    订阅   : {"cmd":"C8","key":..,"list":[{"mid":..}],"cufm":"L",...}  （L=1.5s / LM=4s 节流）

本模块只实现 **RFC6455 帧编解码 + 握手 + 保活** 这三件必需的事，
不引入 `websockets` / `websocket-client` 等依赖，保持 collector 层零依赖
（AGENTS.md §3.2 与 pyproject.toml 的依赖契约）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import select
import socket
import ssl
import struct
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .leyu_client import (
    WS_HEARTBEAT_INTERVAL_S,
    WS_HEARTBEAT_TIMEOUT_S,
    WS_RECONNECT_INTERVAL_S,
    DecodeError,
    TransportError,
    ws_heartbeat,
    ws_subscribe_odds,
)

__all__ = [
    "OPCODE_CONT",
    "OPCODE_TEXT",
    "OPCODE_BINARY",
    "OPCODE_CLOSE",
    "OPCODE_PING",
    "OPCODE_PONG",
    "Frame",
    "WebSocketConnection",
    "LEYUFeed",
    "encode_frame",
    "decode_frame",
    "build_handshake",
    "ws_accept_key",
    "WS_GUID",
]

# RFC6455 §5.2 操作码
OPCODE_CONT = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA

# 单帧载荷上限（协议允许 2^63，这里给一个防御性上限，避免恶意长度撑爆内存）
MAX_FRAME_BYTES = 16 * 1024 * 1024
_NO_MASK_BIT = 0x80
_LEN_16 = 126
_LEN_64 = 127


# RFC6455 §1.3 规定的握手 GUID
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_accept_key(sec_websocket_key: str) -> str:
    """计算 `Sec-WebSocket-Accept`（RFC6455 §4.2.2）。

    算法由协议**强制**规定为 SHA-1：`base64(SHA1(key + GUID))`。
    这里 SHA-1 是握手协议要求，不是密码学安全用途，不可改用 SHA-256，
    否则握手将不被任何合规服务端接受。用 `hashlib.new` 表达以明确
    "这是协议指定算法"而非安全哈希选择。
    """
    digest = hashlib.new("sha1", (sec_websocket_key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


# --------------------------------------------------------------------------- #
# 帧编解码（纯函数，可单测）
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Frame:
    """一个已解析的 WebSocket 帧。"""

    fin: bool
    opcode: int
    payload: bytes

    @property
    def text(self) -> str:
        return self.payload.decode("utf-8", "replace")


def encode_frame(
    payload: bytes,
    opcode: int = OPCODE_TEXT,
    mask: bool = True,
    fin: bool = True,
    mask_key: Optional[bytes] = None,
) -> bytes:
    """编码一个 WebSocket 帧。

    Args:
        payload: 载荷字节。
        opcode: 操作码。
        mask: 是否加掩码。**客户端发往服务端必须为 True**（RFC6455 §5.1），
            服务端发往客户端必须为 False，故做成参数以便测试双向。
        fin: 是否结束帧。
        mask_key: 指定掩码（测试可复现）；默认随机 4 字节。
    """
    header = bytearray()
    header.append((0x80 if fin else 0x00) | (opcode & 0x0F))
    length = len(payload)
    mask_bit = _NO_MASK_BIT if mask else 0x00
    if length < _LEN_16:
        header.append(mask_bit | length)
    elif length < 1 << 16:
        header.append(mask_bit | _LEN_16)
        header.extend(struct.pack("!H", length))
    else:
        header.append(mask_bit | _LEN_64)
        header.extend(struct.pack("!Q", length))

    if not mask:
        return bytes(header) + payload
    key = mask_key if mask_key is not None else os.urandom(4)
    if len(key) != 4:
        raise ValueError("掩码必须是 4 字节，实际 %d" % len(key))
    masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return bytes(header) + key + masked


def decode_frame(buffer: bytes) -> Optional[Tuple[Frame, int]]:
    """从缓冲区头部解析一个帧。

    Returns:
        `(Frame, consumed_bytes)`；数据不完整时返回 None（调用方继续读）。
    Raises:
        DecodeError: 协议违规（如服务端竟发掩码帧、长度超限）。
    """
    if len(buffer) < 2:
        return None
    b0, b1 = buffer[0], buffer[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & _NO_MASK_BIT)
    length = b1 & 0x7F
    offset = 2

    if length == _LEN_16:
        if len(buffer) < offset + 2:
            return None
        length = struct.unpack("!H", buffer[offset:offset + 2])[0]
        offset += 2
    elif length == _LEN_64:
        if len(buffer) < offset + 8:
            return None
        length = struct.unpack("!Q", buffer[offset:offset + 8])[0]
        offset += 8
    if length > MAX_FRAME_BYTES:
        raise DecodeError("帧长度超限: %d" % length)

    key = b""
    if masked:
        if len(buffer) < offset + 4:
            return None
        key = buffer[offset:offset + 4]
        offset += 4
    if len(buffer) < offset + length:
        return None

    raw = buffer[offset:offset + length]
    if masked:
        raw = bytes(b ^ key[i % 4] for i, b in enumerate(raw))
    return Frame(fin=fin, opcode=opcode, payload=raw), offset + length


def build_handshake(host: str, path: str, origin: str, key: str) -> bytes:
    """构造 WebSocket 升级请求（与抓包 sid 324 的头部一致）。"""
    lines = [
        "GET %s HTTP/1.1" % path,
        "Host: %s" % host,
        "Connection: Upgrade",
        "Pragma: no-cache",
        "Cache-Control: no-cache",
        "Upgrade: websocket",
        "Origin: %s" % origin,
        "Sec-WebSocket-Version: 13",
        "Sec-WebSocket-Key: %s" % key,
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


# --------------------------------------------------------------------------- #
# 连接
# --------------------------------------------------------------------------- #

class WebSocketConnection:
    """阻塞式 WebSocket 连接（含自动掩码与 ping/pong 应答）。"""

    def __init__(self, url: str, origin: str, timeout: float = 20.0) -> None:
        self.url = url
        self.origin = origin
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._buffer = bytearray()
        self._fragments: List[bytes] = []
        self._fragment_opcode = OPCODE_CONT
        self.closed = False

    # -- 连接管理 -----------------------------------------------------------

    def connect(self) -> None:
        """执行 TCP(+TLS) 连接与 HTTP 升级握手。"""
        parsed = urllib.parse.urlsplit(self.url)
        secure = parsed.scheme == "wss"
        host = parsed.hostname or ""
        port = parsed.port or (443 if secure else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        try:
            raw = socket.create_connection((host, port), timeout=self.timeout)
        except OSError as exc:
            raise TransportError("WebSocket 连接失败 %s:%d: %s" % (host, port, exc)) from exc
        if secure:
            ctx = ssl.create_default_context()
            try:
                raw = ctx.wrap_socket(raw, server_hostname=host)
            except ssl.SSLError as exc:
                raw.close()
                raise TransportError("TLS 握手失败 %s: %s" % (host, exc)) from exc
        raw.settimeout(self.timeout)
        self._sock = raw

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        raw.sendall(build_handshake(host, path, self.origin, key))
        status, _headers = self._read_handshake()
        if not status.startswith("101"):
            self.close()
            raise TransportError("WebSocket 升级被拒绝: %s" % status)
        # RFC6455 §4.1：客户端必须校验 Sec-WebSocket-Accept
        expect = ws_accept_key(key)
        got = _headers.get("sec-websocket-accept", "")
        if got and got != expect:
            self.close()
            raise TransportError("Sec-WebSocket-Accept 校验失败: %s" % got)

    def _require_sock(self) -> socket.socket:
        """取得已连接的 socket；未连接则抛错（不用 assert，避免被 -O 剃掉）。"""
        if self._sock is None:
            raise TransportError("连接未建立")
        return self._sock

    def _read_handshake(self) -> Tuple[str, Dict[str, str]]:
        sock = self._require_sock()
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise TransportError("握手期间连接被关闭")
            buf.extend(chunk)
        head, _, rest = bytes(buf).partition(b"\r\n\r\n")
        self._buffer.extend(rest)
        lines = head.decode("latin-1").split("\r\n")
        headers: Dict[str, str] = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            if k:
                headers[k.strip().lower()] = v.strip()
        return lines[0].split(" ", 2)[1] if lines else "", headers

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._sock is not None:
            try:
                self._sock.sendall(encode_frame(b"", OPCODE_CLOSE))
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # -- 收发 ---------------------------------------------------------------

    def send_text(self, text: str) -> None:
        """发送文本帧（客户端方向自动加掩码）。"""
        self._send(encode_frame(text.encode("utf-8"), OPCODE_TEXT))

    def _send(self, data: bytes) -> None:
        if self._sock is None:
            raise TransportError("连接未建立")
        try:
            self._sock.sendall(data)
        except OSError as exc:
            raise TransportError("发送失败: %s" % exc) from exc

    def recv(self, timeout: Optional[float] = None) -> Optional[Frame]:
        """接收下一个数据帧。

        Returns:
            Frame；超时返回 None；收到 close 帧返回 None 并置 closed。
        自动应答 ping（返回 pong）并处理分片重组。
        """
        sock = self._require_sock()
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            parsed = decode_frame(bytes(self._buffer))
            if parsed is not None:
                frame, consumed = parsed
                del self._buffer[:consumed]
                result = self._handle_control(frame)
                if result is not _CONTINUE:
                    return result  # type: ignore[return-value]
                continue

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([sock], [], [], remaining)
            if not ready:
                return None
            try:
                chunk = sock.recv(65536)
            except (socket.timeout, TimeoutError):
                return None
            except OSError as exc:
                raise TransportError("接收失败: %s" % exc) from exc
            if not chunk:
                self.closed = True
                return None
            self._buffer.extend(chunk)

    def _handle_control(self, frame: Frame) -> Optional[Frame]:
        """处理控制帧与分片；返回数据帧则交还调用方，否则返回哨兵 _CONTINUE。"""
        if frame.opcode == OPCODE_PING:
            self._send(encode_frame(frame.payload, OPCODE_PONG))
            return _CONTINUE  # type: ignore[return-value]
        if frame.opcode == OPCODE_PONG:
            return _CONTINUE  # type: ignore[return-value]
        if frame.opcode == OPCODE_CLOSE:
            self.closed = True
            return None
        if frame.opcode == OPCODE_CONT:
            self._fragments.append(frame.payload)
            if frame.fin:
                payload = b"".join(self._fragments)
                opcode = self._fragment_opcode
                self._fragments = []
                return Frame(fin=True, opcode=opcode, payload=payload)
            return _CONTINUE  # type: ignore[return-value]
        if not frame.fin:
            self._fragments = [frame.payload]
            self._fragment_opcode = frame.opcode
            return _CONTINUE  # type: ignore[return-value]
        return frame


class _Continue:
    """内部哨兵：表示该帧已被消费（控制帧/未完成分片），调用方应继续读。"""


_CONTINUE = _Continue()


# --------------------------------------------------------------------------- #
# 乐鱼实时源
# --------------------------------------------------------------------------- #

class LEYUFeed:
    """乐鱼实时推送订阅器：自动握手 + 心跳 + 订阅 + 断线重连。

    用法::

        feed = LEYUFeed("wss://api.example/yewuws2/push?requestId=xx", origin=...,
                        request_id="xx")
        feed.subscribe_odds(["5714088", "5687518"])
        for message in feed:
            print(message)

    回调模式::

        feed = LEYUFeed(..., on_message=lambda m: print(m["cmd"]))
        feed.run_forever()          # 阻塞，直到 stop()
    """

    def __init__(
        self,
        url: str,
        request_id: str,
        origin: str,
        timeout: float = 20.0,
        heartbeat_interval: float = WS_HEARTBEAT_INTERVAL_S,
        heartbeat_timeout: float = WS_HEARTBEAT_TIMEOUT_S,
        reconnect_interval: float = WS_RECONNECT_INTERVAL_S,
        on_message: Optional[Callable[[Dict[str, Any]], None]] = None,
        auto_reconnect: bool = True,
    ) -> None:
        self.url = url
        self.request_id = request_id
        self.origin = origin
        self.timeout = timeout
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.reconnect_interval = reconnect_interval
        self.on_message = on_message
        self.auto_reconnect = auto_reconnect

        self._conn: Optional[WebSocketConnection] = None
        self._pending: List[str] = []
        self._stop = threading.Event()
        self._last_recv = time.monotonic()
        self._hb_thread: Optional[threading.Thread] = None
        self.stats: Dict[str, int] = {"connected": 0, "messages": 0, "reconnects": 0}

    # -- 生命周期 -----------------------------------------------------------

    def connect(self) -> None:
        """建立连接并在 open 后重放已登记的订阅。"""
        conn = WebSocketConnection(self.url, self.origin, self.timeout)
        conn.connect()
        self._conn = conn
        self._last_recv = time.monotonic()
        self.stats["connected"] += 1
        self._start_heartbeat()
        for payload in self._pending:
            conn.send_text(payload)

    def _start_heartbeat(self) -> None:
        if self._hb_thread is not None and self._hb_thread.is_alive():
            return
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._hb_thread.start()

    def _heartbeat_loop(self) -> None:
        """每 5s 发心跳；超过 8s 无任何下行即认为链路失效并触发重连。"""
        while not self._stop.is_set():
            if self._stop.wait(self.heartbeat_interval):
                return
            conn = self._conn
            if conn is None or conn.closed:
                continue
            if time.monotonic() - self._last_recv > self.heartbeat_timeout:
                try:
                    conn.close()
                finally:
                    self._conn = None
                continue
            try:
                conn.send_text(ws_heartbeat(self.request_id))
            except TransportError:
                self._conn = None

    def close(self) -> None:
        self._stop.set()
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "LEYUFeed":
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc_value: Optional[BaseException],
        traceback: Optional[object],
    ) -> None:
        self.close()

    # -- 订阅 ---------------------------------------------------------------

    def subscribe_odds(
        self,
        mids: List[str],
        key: str = "leyu",
        cufm: str = "L",
        market_level: int = 0,
        es_market_level: int = 0,
        odds_type: Optional[str] = None,
    ) -> str:
        """登记并发送盘口订阅（C8）。连接未就绪时先缓存，open 后重放。"""
        payload = ws_subscribe_odds(
            self.request_id, key, mids, cufm=cufm,
            market_level=market_level, es_market_level=es_market_level,
            odds_type=odds_type,
        )
        if payload not in self._pending:
            self._pending.append(payload)
        if self._conn is not None and not self._conn.closed:
            self._conn.send_text(payload)
        return payload

    def subscribe_match(self, mid: str) -> None:
        """赛事详情页订阅（C13 + C4），与前端动画页行为一致。"""
        for payload in (
            json.dumps({"cmd": "C13", "mid": str(mid), "requestId": self.request_id}),
            json.dumps({"cmd": "C4", "uuid": "%s_Z01" % self.request_id,
                        "requestId": self.request_id}),
        ):
            if payload not in self._pending:
                self._pending.append(payload)
            if self._conn is not None and not self._conn.closed:
                self._conn.send_text(payload)

    # -- 迭代 ---------------------------------------------------------------

    def __iter__(self) -> "LEYUFeed":
        return self

    def __next__(self) -> Dict[str, Any]:
        message = self.recv()
        if message is None:
            raise StopIteration
        return message

    def recv(self) -> Optional[Dict[str, Any]]:
        """阻塞取一条 JSON 消息；链路断开时按策略重连并继续。"""
        while not self._stop.is_set():
            conn = self._conn
            if conn is None or conn.closed:
                if not self.auto_reconnect:
                    return None
                self.stats["reconnects"] += 1
                if self._stop.wait(self.reconnect_interval):
                    return None
                try:
                    self.connect()
                except TransportError:
                    continue
                conn = self._conn
                if conn is None:
                    continue
            frame = conn.recv(timeout=self.heartbeat_timeout)
            if frame is None:
                conn.close()
                if self._conn is conn:
                    self._conn = None
                continue
            self._last_recv = time.monotonic()
            if frame.opcode not in (OPCODE_TEXT, OPCODE_BINARY):
                continue
            self.stats["messages"] += 1
            try:
                data = json.loads(frame.text)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
        return None

    def run_forever(self) -> None:
        """持续消费并回调，直到 close()。"""
        while not self._stop.is_set():
            message = self.recv()
            if message is None:
                break
            if self.on_message is not None:
                self.on_message(message)


def _random_request_id() -> str:
    """生成与前端一致的 32 位 hex requestId。"""
    return "%032x" % random.getrandbits(128)
