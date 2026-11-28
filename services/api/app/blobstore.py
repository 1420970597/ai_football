from __future__ import annotations

import asyncio
import atexit
import logging
import os
import threading
from io import BytesIO
import warnings

import boto3
from botocore.config import Config
from PIL import Image


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_FRAMES = 100
MAX_ANIMATION_PIXELS = 80_000_000
_clients_local = threading.local()
_clients_lock = threading.Lock()
_clients = {}
_clients_generation = 0


def validate_image(data: bytes) -> dict:
    mime = image_mime(data)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                width, height = image.size
                frames = getattr(image, 'n_frames', 1)
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise RuntimeError('remote image pixel limit exceeded')
                if frames > MAX_IMAGE_FRAMES or width * height * frames > MAX_ANIMATION_PIXELS:
                    raise RuntimeError('remote image animation limit exceeded')
                actual = Image.MIME.get(image.format)
                if actual != mime:
                    raise RuntimeError('remote image decoder type conflicts with signature')
                image.verify()
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError('remote image decoding failed') from exc
    return {'mime_type': mime, 'width': width, 'height': height, 'frames': frames}


def image_mime(data: bytes) -> str:
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data[:6] in {b'GIF87a', b'GIF89a'}:
        return 'image/gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    raise RuntimeError('remote image has no supported image signature')


def _s3_client():
    endpoint = os.environ.get("S3_ENDPOINT_URL", "").strip()
    access_key = os.environ.get("S3_ACCESS_KEY", "").strip()
    secret_key = os.environ.get("S3_SECRET_KEY", "")
    if not all((endpoint, access_key, secret_key)):
        _retire_local_client()
        raise RuntimeError("remote S3 media storage is not configured")
    region = os.environ.get("S3_REGION", "us-east-1")
    use_ssl = os.environ.get("S3_USE_TLS", "true").lower() == "true"
    proxy = os.environ.get("S3_HTTPS_PROXY", "")
    proxy_environment = tuple(os.environ.get(key, '') for key in (
        'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
        'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy', 'AWS_CA_BUNDLE'))
    key = (os.getpid(), endpoint, access_key, secret_key, region, use_ssl, proxy, proxy_environment)
    cached = getattr(_clients_local, 'entry', None)
    if cached and cached[0] == key and cached[1] == _clients_generation:
        return cached[2]
    _retire_local_client()
    # A Session belongs to this thread; never race boto3's default session.
    client = boto3.session.Session().client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        use_ssl=use_ssl,
        config=Config(connect_timeout=3, read_timeout=8, retries={"max_attempts": 2},
                      proxies={"https": proxy} if proxy else None),
    )
    with _clients_lock:
        _clients[id(client)] = client
        _clients_local.entry = (key, _clients_generation, client)
    return client


def _retire_local_client():
    cached = getattr(_clients_local, 'entry', None)
    if cached is not None:
        del _clients_local.entry
        with _clients_lock:
            client = _clients.pop(id(cached[2]), None)
        if client is not None:
            client.close()


def close_clients():
    """Release connection pools after executor threads have drained at exit."""
    global _clients_generation
    with _clients_lock:
        clients = list(_clients.values())
        _clients.clear()
        _clients_generation += 1
    for client in clients:
        try:
            client.close()
        except Exception as exc:
            logging.getLogger(__name__).warning('media client close failed category=%s', type(exc).__name__)


atexit.register(close_clients)


def _read_object(key: str) -> bytes:
    bucket = os.environ.get("S3_BUCKET", "").strip()
    if not bucket or not key or key.startswith("/") or ".." in key.split("/"):
        raise RuntimeError("invalid remote media object reference")
    response = _s3_client().get_object(Bucket=bucket, Key=key)
    stream = response["Body"]
    try:
        data = stream.read(MAX_IMAGE_BYTES + 1)
    finally:
        stream.close()
    if len(data) > MAX_IMAGE_BYTES:
        raise RuntimeError("Telegram image exceeds the analysis size limit")
    return data


async def read_image(key: str) -> bytes:
    data = await asyncio.to_thread(_read_object, key)
    await asyncio.to_thread(validate_image, data)
    return data
