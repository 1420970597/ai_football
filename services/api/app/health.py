"""Short, cached process probes; provider success is a separate fact."""

from __future__ import annotations

import asyncio
import hashlib
import time
import os
import uuid

import boto3
import httpx
from botocore.config import Config


class HermesProbe:
    def __init__(self, api_url: str, *, ttl: float = 15):
        self.url = api_url.removesuffix("/v1").rstrip("/") + "/health"
        self.ttl = ttl
        self.lock = asyncio.Lock()
        self.expires = 0.0
        self.value = "unavailable"

    async def check(self) -> str:
        if time.monotonic() < self.expires:
            return self.value
        async with self.lock:
            if time.monotonic() < self.expires:
                return self.value
            try:
                async with httpx.AsyncClient(timeout=1.5, trust_env=False, follow_redirects=False) as client:
                    response = await client.get(self.url)
                    self.value = "reachable" if response.status_code == 200 else "unavailable"
            except httpx.HTTPError:
                self.value = "unavailable"
            self.expires = time.monotonic() + self.ttl
            return self.value


class StorageProbe:
    """Bounded S3 read/write probe using a namespaced, disposable object."""

    def __init__(self, *, ttl: float = 30):
        self.ttl = ttl
        self.lock = asyncio.Lock()
        self.expires = 0.0
        self.value = "unavailable"

    def _check_sync(self) -> str:
        endpoint = os.environ.get("S3_ENDPOINT_URL", "").strip()
        bucket = os.environ.get("S3_BUCKET", "").strip()
        access = os.environ.get("S3_ACCESS_KEY", "").strip()
        secret = os.environ.get("S3_SECRET_KEY", "")
        if not all((endpoint, bucket, access, secret)):
            return "unconfigured"
        key = f"{os.environ.get('S3_OBJECT_PREFIX', 'v2/').strip('/')}/health/{uuid.uuid4().hex}"
        body = b"ai-football-health-probe-v1"
        client = boto3.client(
            "s3", endpoint_url=endpoint, region_name=os.environ.get("S3_REGION", "us-east-1"),
            aws_access_key_id=access, aws_secret_access_key=secret,
            use_ssl=os.environ.get("S3_USE_TLS", "true").lower() == "true",
            config=Config(connect_timeout=2, read_timeout=5, retries={"max_attempts": 1},
                          proxies={"https": os.environ["S3_HTTPS_PROXY"]} if os.environ.get("S3_HTTPS_PROXY") else None),
        )
        try:
            client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/octet-stream")
            response = client.get_object(Bucket=bucket, Key=key)
            stream = response["Body"]
            try:
                received = stream.read(len(body) + 1)
            finally:
                stream.close()
            if received != body or hashlib.sha256(received).hexdigest() != hashlib.sha256(body).hexdigest():
                return "unavailable"
            return "healthy"
        except Exception:
            return "unavailable"
        finally:
            try:
                client.delete_object(Bucket=bucket, Key=key)
            except Exception:
                pass
            client.close()

    async def check(self) -> str:
        if time.monotonic() < self.expires:
            return self.value
        async with self.lock:
            if time.monotonic() < self.expires:
                return self.value
            self.value = await asyncio.to_thread(self._check_sync)
            self.expires = time.monotonic() + self.ttl
            return self.value


class MCPProbe:
    def __init__(self, api_url: str, token: str, *, ttl: float = 15):
        self.url = api_url.rstrip("/")
        self.token = token
        self.ttl = ttl
        self.lock = asyncio.Lock()
        self.expires = 0.0
        self.value = "unavailable"

    async def check(self) -> str:
        if not self.token:
            return "unconfigured"
        if time.monotonic() < self.expires:
            return self.value
        async with self.lock:
            if time.monotonic() < self.expires:
                return self.value
            try:
                async with httpx.AsyncClient(timeout=1.5, trust_env=False, follow_redirects=False) as client:
                    async with client.stream("GET", self.url, headers={
                        "Authorization": "Bearer " + self.token,
                        "Accept": "text/event-stream",
                    }) as response:
                        # Do not consume the potentially unbounded SSE body.
                        self.value = "reachable" if response.status_code in {200, 405} else "unavailable"
            except httpx.HTTPError:
                self.value = "unavailable"
            self.expires = time.monotonic() + self.ttl
            return self.value
