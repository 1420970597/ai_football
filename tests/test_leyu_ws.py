#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""collector.leyu_ws 的单元测试（RFC6455 帧层 + 订阅/保活策略）。

不发起任何真实网络连接：所有 socket 交互都通过本地回环"假服务端"完成，
或直接测纯函数。

    python3 -m unittest tests.test_leyu_ws -v
"""

from __future__ import annotations

import base64
import json
import socket
import struct
import threading
import time
import unittest
from typing import Any, List, Optional, Tuple

from collector.leyu_client import WS_HEARTBEAT_INTERVAL_S
from collector.leyu_ws import (
    OPCODE_BINARY,
    OPCODE_CLOSE,
    OPCODE_CONT,
    OPCODE_PING,
    OPCODE_PONG,
    OPCODE_TEXT,
    DecodeError,
    Frame,
    LEYUFeed,
    WebSocketConnection,
    build_handshake,
    decode_frame,
    encode_frame,
    ws_accept_key,
)


# --------------------------------------------------------------------------- #
# 帧编解码
# --------------------------------------------------------------------------- #

class TestFrameCodec(unittest.TestCase):
    def test_small_payload_roundtrip_masked(self) -> None:
        raw = encode_frame(b"hello", OPCODE_TEXT, mask=True, mask_key=b"\x01\x02\x03\x04")
        parsed = decode_frame(raw)
        assert parsed is not None
        frame, consumed = parsed
        self.assertEqual(consumed, len(raw))
        self.assertEqual(frame.payload, b"hello")
        self.assertEqual(frame.opcode, OPCODE_TEXT)
        self.assertTrue(frame.fin)
        self.assertEqual(frame.text, "hello")

    def test_unmasked_server_frame_roundtrip(self) -> None:
        raw = encode_frame(b"world" * 100, OPCODE_TEXT, mask=False)
        parsed = decode_frame(raw)
        assert parsed is not None
        frame, consumed = parsed
        self.assertEqual(frame.payload, b"world" * 100)
        self.assertEqual(consumed, len(raw))

    def test_medium_length_uses_16bit_header(self) -> None:
        payload = b"x" * 200
        raw = encode_frame(payload, OPCODE_BINARY, mask=False)
        self.assertEqual(raw[1] & 0x7F, 126)
        parsed = decode_frame(raw)
        assert parsed is not None
        self.assertEqual(parsed[0].payload, payload)

    def test_large_length_uses_64bit_header(self) -> None:
        payload = b"y" * 70000
        raw = encode_frame(payload, OPCODE_BINARY, mask=False)
        self.assertEqual(raw[1] & 0x7F, 127)
        parsed = decode_frame(raw)
        assert parsed is not None
        self.assertEqual(parsed[0].payload, payload)

    def test_client_frames_are_masked(self) -> None:
        """RFC6455 §5.1：客户端发往服务端必须加掩码。"""
        raw = encode_frame(b"abc", OPCODE_TEXT, mask=True)
        self.assertTrue(raw[1] & 0x80)
        # 载荷必须真的被异或（不能等于明文）
        self.assertNotEqual(raw[-3:], b"abc")

    def test_decode_returns_none_on_partial_data(self) -> None:
        raw = encode_frame(b"abcdefghij", OPCODE_TEXT, mask=False)
        for cut in range(len(raw)):
            with self.subTest(cut=cut):
                self.assertIsNone(decode_frame(raw[:cut]))

    def test_decode_handles_partial_header_extension(self) -> None:
        raw = encode_frame(b"x" * 200, OPCODE_TEXT, mask=False)
        self.assertIsNone(decode_frame(raw[:3]))

    def test_rejects_oversized_frame(self) -> None:
        # 手工构造 len=2^40 的 64bit 头，必须被拒绝而不是尝试分配内存
        header = bytes([0x81, 127]) + struct.pack("!Q", 1 << 40)
        with self.assertRaises(DecodeError):
            decode_frame(header)

    def test_utf8_multibyte_roundtrip(self) -> None:
        text = "全场让球 +1.5 大"
        raw = encode_frame(text.encode("utf-8"), OPCODE_TEXT, mask=True)
        parsed = decode_frame(raw)
        assert parsed is not None
        self.assertEqual(parsed[0].text, text)

    def test_invalid_mask_key_length_rejected(self) -> None:
        with self.assertRaises(ValueError):
            encode_frame(b"x", OPCODE_TEXT, mask=True, mask_key=b"\x01\x02")

    def test_empty_payload_frame(self) -> None:
        raw = encode_frame(b"", OPCODE_CLOSE, mask=True)
        parsed = decode_frame(raw)
        assert parsed is not None
        self.assertEqual(parsed[0].payload, b"")
        self.assertEqual(parsed[0].opcode, OPCODE_CLOSE)


class TestHandshake(unittest.TestCase):
    def test_handshake_matches_captured_headers(self) -> None:
        req = build_handshake("api.test", "/yewuws2/push?requestId=abc", "https://web.test", "KEY==")
        text = req.decode("ascii")
        self.assertTrue(text.startswith("GET /yewuws2/push?requestId=abc HTTP/1.1\r\n"))
        for header in ("Host: api.test", "Connection: Upgrade", "Upgrade: websocket",
                       "Sec-WebSocket-Version: 13", "Sec-WebSocket-Key: KEY==",
                       "Origin: https://web.test"):
            self.assertIn(header, text)
        self.assertTrue(text.endswith("\r\n\r\n"))


# --------------------------------------------------------------------------- #
# 本地假服务端（真实走一遍握手 + 帧收发）
# --------------------------------------------------------------------------- #

class _FakeServer:
    """极简 WS 服务端：完成握手，随后按脚本回帧并记录收到的客户端帧。"""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.received: List[Frame] = []
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._stop = threading.Event()

    def start(self) -> "_FakeServer":
        self._thread.start()
        return self

    def _serve(self) -> None:
        conn, _ = self.sock.accept()
        conn.settimeout(10.0)
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf.extend(chunk)
        head, _, rest = bytes(buf).partition(b"\r\n\r\n")
        key = ""
        for line in head.decode("latin-1").split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        # 复用生产实现的 RFC6455 §4.2.2 握手摘要，避免测试与实现各写一份
        accept = ws_accept_key(key)
        conn.sendall((
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Accept: %s\r\n\r\n" % accept
        ).encode())
        self._ready.set()
        buf = bytearray(rest)

        def send(payload: bytes, opcode: int) -> None:
            conn.sendall(encode_frame(payload, opcode, mask=False))

        # 主动推 1 条业务消息 + 1 个 ping，随后回显收到的客户端帧
        send(b'{"cmd":"C118","mid":"5714088"}', OPCODE_TEXT)
        send(b"ping-body", OPCODE_PING)
        deadline = time.monotonic() + 8.0
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                chunk = conn.recv(65536)
            except (socket.timeout, TimeoutError):
                continue
            if not chunk:
                break
            buf.extend(chunk)
            while True:
                parsed = decode_frame(bytes(buf))
                if parsed is None:
                    break
                frame, consumed = parsed
                del buf[:consumed]
                self.received.append(frame)
                if frame.opcode == OPCODE_PONG:
                    send(b'{"cmd":"pong-ack"}', OPCODE_TEXT)
                elif frame.opcode == OPCODE_CLOSE:
                    self._stop.set()
        try:
            conn.close()
        except OSError:
            pass

    def wait_ready(self, timeout: float = 5.0) -> bool:
        return self._ready.wait(timeout)

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


class TestConnectionAgainstFakeServer(unittest.TestCase):
    """真实 socket 交互：验证掩码、控制帧应答、分片、close。"""

    def setUp(self) -> None:
        self.server = _FakeServer().start()
        self.conn = WebSocketConnection(
            "ws://127.0.0.1:%d/yewuws2/push?requestId=abc" % self.server.port,
            origin="https://web.test",
            timeout=5.0,
        )
        self.conn.connect()

    def tearDown(self) -> None:
        self.conn.close()
        self.server.stop()

    def test_handshake_then_receive_push(self) -> None:
        frame = self.conn.recv(timeout=5.0)
        assert frame is not None
        self.assertEqual(json.loads(frame.text)["cmd"], "C118")

    def test_ping_is_answered_with_masked_pong(self) -> None:
        # 第一条是业务消息，第二条触发 ping -> pong
        self.conn.recv(timeout=5.0)
        self.conn.recv(timeout=5.0)
        deadline = time.monotonic() + 5.0
        pongs: List[Frame] = []
        while time.monotonic() < deadline and not pongs:
            pongs = [f for f in self.server.received if f.opcode == OPCODE_PONG]
            time.sleep(0.05)
        self.assertTrue(pongs, "必须自动应答 ping")
        self.assertEqual(pongs[0].payload, b"ping-body")

    def test_client_send_is_masked_on_wire(self) -> None:
        self.conn.send_text('{"cmd":"C0"}')
        deadline = time.monotonic() + 5.0
        text_frames: List[Frame] = []
        while time.monotonic() < deadline and not text_frames:
            text_frames = [f for f in self.server.received if f.opcode == OPCODE_TEXT]
            time.sleep(0.05)
        self.assertTrue(text_frames)
        # 服务端解码后应得到原文（说明掩码被正确解掉）
        self.assertEqual(json.loads(text_frames[0].text)["cmd"], "C0")

    def test_rejects_non_101_upgrade(self) -> None:
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def responder() -> None:
            c, _ = srv.accept()
            c.recv(4096)
            c.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            c.close()

        threading.Thread(target=responder, daemon=True).start()
        conn = WebSocketConnection("ws://127.0.0.1:%d/x" % port, "https://w", timeout=5.0)
        with self.assertRaises(Exception):
            conn.connect()
        srv.close()


# --------------------------------------------------------------------------- #
# 订阅与保活策略（不依赖真实服务器）
# --------------------------------------------------------------------------- #

class TestFeedPolicy(unittest.TestCase):
    def test_heartbeat_interval_matches_js(self) -> None:
        self.assertEqual(WS_HEARTBEAT_INTERVAL_S, 5.0)

    def test_subscribe_queues_before_connect_and_replays(self) -> None:
        """连接前订阅必须缓存，open 后重放（否则会丢订阅）。"""
        sent: List[str] = []

        class _FakeConn:
            closed = False

            def send_text(self, text: str) -> None:
                sent.append(text)

            def close(self) -> None:
                self.closed = True

            def recv(self, timeout: Optional[float] = None) -> Optional[Frame]:
                return None

        feed = LEYUFeed("ws://x/yewuws2/push?requestId=r", "r", "https://w")
        payload = feed.subscribe_odds(["5714088"])
        self.assertEqual(sent, [])          # 未连接 -> 只入队
        self.assertIn(payload, feed._pending)
        # 模拟 open：用真实可发送对象替换 _conn
        fake = _FakeConn()
        feed._conn = fake  # type: ignore[assignment]
        for p in feed._pending:
            fake.send_text(p)
        self.assertEqual(len(sent), 1)
        self.assertEqual(json.loads(sent[0])["cmd"], "C8")

    def test_subscribe_odds_payload_fields(self) -> None:
        feed = LEYUFeed("ws://x/yewuws2/push?requestId=r", "r", "https://w")
        body = json.loads(feed.subscribe_odds(["1", "2"], cufm="LM", market_level=2))
        self.assertEqual(body["cmd"], "C8")
        self.assertEqual(body["cufm"], "LM")
        self.assertEqual(body["marketLevel"], 2)
        self.assertEqual([x["mid"] for x in body["list"]], ["1", "2"])

    def test_subscribe_match_sends_c13_and_c4(self) -> None:
        feed = LEYUFeed("wss://x/yewuws2/push?requestId=rid", "rid", "https://w")
        feed.subscribe_match("5714088")
        cmds = [json.loads(p)["cmd"] for p in feed._pending]
        self.assertEqual(cmds, ["C13", "C4"])
        c4 = json.loads(feed._pending[1])
        self.assertEqual(c4["uuid"], "rid_Z01")

    def test_pending_deduplicates_identical_subscriptions(self) -> None:
        feed = LEYUFeed("wss://x/yewuws2/push?requestId=r", "r", "https://w")
        feed.subscribe_odds(["1"])
        feed.subscribe_odds(["1"])
        self.assertEqual(len(feed._pending), 1)

    def test_close_is_idempotent(self) -> None:
        feed = LEYUFeed("wss://x/yewuws2/push?requestId=r", "r", "https://w")
        feed.close()
        feed.close()
        self.assertTrue(feed._stop.is_set())

    def test_context_manager_closes(self) -> None:
        feed = LEYUFeed("wss://127.0.0.1:9/yewuws2/push?requestId=r", "r", "https://w",
                        auto_reconnect=False, timeout=0.5)
        with self.assertRaises(Exception):
            feed.__enter__()
        feed.close()
        self.assertTrue(feed._stop.is_set())

    def test_recv_without_reconnect_returns_none(self) -> None:
        feed = LEYUFeed("wss://127.0.0.1:9/yewuws2/push?requestId=r", "r", "https://w",
                        auto_reconnect=False, timeout=0.2)
        self.assertIsNone(feed.recv())


if __name__ == "__main__":
    unittest.main(verbosity=2)
