"""仅内网使用的鉴权控制端点；扫码和二步密码不写磁盘或数据库。"""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import threading


class ControlServer:
    def __init__(self, collector, token: str, host: str = "0.0.0.0", port: int = 8092):
        if not token:
            raise RuntimeError("Telegram control service token is required")
        self.collector = collector
        self.token = token
        self.loop = asyncio.get_running_loop()
        self.gate = threading.BoundedSemaphore(16)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                # 请求体可能有短期登录密码，不记录请求内容和 URL 参数。
                return

            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def do_POST(self):
                if not outer.gate.acquire(blocking=False):
                    self.send_error(503)
                    return
                future = None
                try:
                    supplied = self.headers.get("Authorization", "")
                    if not hmac.compare_digest(supplied.encode(), ("Bearer " + outer.token).encode()):
                        self.send_error(401)
                        return
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 0 or length > 16384 or self.headers.get("Transfer-Encoding"):
                        self.send_error(413)
                        return
                    if self.path not in {"/status", "/auth/qr", "/auth/password", "/auth/cancel", "/dialogs", "/admins", "/members", "/disconnect", "/leyu/refresh"}:
                        self.send_error(404)
                        return
                    raw = self.rfile.read(length)
                    body = json.loads(raw or b"{}")
                    if not isinstance(body, dict):
                        raise ValueError("invalid body")
                    future = asyncio.run_coroutine_threadsafe(outer.dispatch(self.path, body), outer.loop)
                    result = future.result(timeout=25)
                    encoded = json.dumps(result, ensure_ascii=False).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                except (ValueError, UnicodeError):
                    self.send_error(400)
                except Exception:
                    if future:
                        future.cancel()
                    self.send_error(503)
                finally:
                    outer.gate.release()

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="telegram-control", daemon=True)

    async def dispatch(self, path, body):
        if path == "/leyu/refresh":
            callback = getattr(self.collector, "leyu_refresh", None)
            if callback is None:
                return {"accepted": False, "written": False, "written_fixtures": 0, "reason": "refresh_unavailable"}
            return await callback(body)
        return await self.collector.control(path, body)

    def start(self):
        self.thread.start()

    async def close(self):
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        await asyncio.to_thread(self.thread.join, 3)
