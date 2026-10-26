"""固定主机密钥 SSH 转发和严格目标白名单 S3 CONNECT 代理。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import select
import signal
import socket
import socketserver
import subprocess
import sys
import threading


MAX_HEADER_BYTES = 8192
MAX_CONNECTION_BYTES = 64 * 1024 * 1024
IDLE_TIMEOUT = 30
CONCURRENT_CONNECTIONS = 16


@dataclass(frozen=True)
class Config:
    ssh_config: str
    ssh_host: str
    pg_remote_host: str
    pg_remote_port: int
    pg_local_port: int
    s3_remote_host: str
    s3_remote_port: int
    s3_forward_port: int
    s3_proxy_port: int
    s3_authority: str

    @classmethod
    def load(cls, path: str) -> "Config":
        values = json.loads(Path(path).read_text())
        value = cls(**values)
        for port in (value.pg_remote_port, value.pg_local_port, value.s3_remote_port, value.s3_forward_port, value.s3_proxy_port):
            if not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError("invalid tunnel port")
        for item in (value.ssh_host, value.pg_remote_host, value.s3_remote_host, value.s3_authority):
            if not item or any(char.isspace() for char in item) or item.startswith("-"):
                raise ValueError("invalid tunnel destination")
        return value


def ssh_command(config: Config) -> list[str]:
    return [
        "ssh", "-F", config.ssh_config, "-NT", "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes", "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
        "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR",
        "-L", f"0.0.0.0:{config.pg_local_port}:{config.pg_remote_host}:{config.pg_remote_port}",
        "-L", f"127.0.0.1:{config.s3_forward_port}:{config.s3_remote_host}:{config.s3_remote_port}",
        config.ssh_host,
    ]


def parse_connect(header: bytes, allowed_authority: str) -> None:
    try:
        text = header.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid CONNECT header") from exc
    lines = text.split("\r\n")
    if lines[0] != f"CONNECT {allowed_authority} HTTP/1.1" and lines[0] != f"CONNECT {allowed_authority} HTTP/1.0":
        raise ValueError("CONNECT destination is not authorized")
    # 明确拒绝携带请求体/转发代理认证，避免请求分割和秘密日志。
    for line in lines[1:]:
        if not line:
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise ValueError("invalid proxy header")
        if key.lower() in {"content-length", "transfer-encoding", "proxy-authorization"}:
            raise ValueError("unsupported proxy header")
        if key.lower() == "host" and value.strip() != allowed_authority:
            raise ValueError("proxy host mismatch")


def relay(left: socket.socket, right: socket.socket) -> None:
    transferred = 0
    while transferred < MAX_CONNECTION_BYTES:
        ready, _, _ = select.select([left, right], [], [], IDLE_TIMEOUT)
        if not ready:
            return
        for source in ready:
            chunk = source.recv(min(65536, MAX_CONNECTION_BYTES - transferred))
            if not chunk:
                return
            transferred += len(chunk)
            target = right if source is left else left
            target.sendall(chunk)


class ConnectHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        if not self.server.slots.acquire(blocking=False):
            self.request.sendall(b"HTTP/1.1 503 Busy\r\nConnection: close\r\n\r\n")
            return
        try:
            self.request.settimeout(5)
            header = bytearray()
            while not header.endswith(b"\r\n\r\n") and len(header) <= MAX_HEADER_BYTES:
                value = self.request.recv(1)
                if not value:
                    return
                header.extend(value)
            if len(header) > MAX_HEADER_BYTES:
                raise ValueError("proxy header too large")
            parse_connect(bytes(header), self.server.allowed_authority)
            with socket.create_connection(("127.0.0.1", self.server.forward_port), timeout=5) as upstream:
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self.request.settimeout(IDLE_TIMEOUT)
                upstream.settimeout(IDLE_TIMEOUT)
                relay(self.request, upstream)
        except ValueError:
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
        except (OSError, TimeoutError):
            # 不回显目标、认证头、请求字节或远端异常。
            pass
        finally:
            self.server.slots.release()


class ConnectServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], allowed_authority: str, forward_port: int):
        self.allowed_authority = allowed_authority
        self.forward_port = forward_port
        self.slots = threading.BoundedSemaphore(CONCURRENT_CONNECTIONS)
        super().__init__(address, ConnectHandler)


def health(config: Config) -> int:
    for port in (config.pg_local_port, config.s3_forward_port, config.s3_proxy_port):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
        except OSError:
            return 1
    return 0


def run(config: Config) -> int:
    process = subprocess.Popen(ssh_command(config), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stopping = threading.Event()

    def terminate(signum, frame) -> None:
        stopping.set()
        process.terminate()

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        with ConnectServer(("0.0.0.0", config.s3_proxy_port), config.s3_authority, config.s3_forward_port) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            print("storage tunnel supervisor started", flush=True)
            while not stopping.wait(1):
                if process.poll() is not None:
                    print("storage tunnel SSH disconnected", flush=True)
                    return 1
            server.shutdown()
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    return 0


def main() -> int:
    try:
        config = Config.load(os.environ.get("STORAGE_TUNNEL_CONFIG", "/run/storage/tunnel.json"))
        return health(config) if sys.argv[1:] == ["health"] else run(config)
    except Exception as exc:
        print(f"storage tunnel unavailable: {type(exc).__name__}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
