from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        parameters = (int(n), int(r), int(p))
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        # 损坏或被篡改的参数不能触发无上限资源消耗。
        if parameters != (2**14, 8, 1) or len(salt) != 16 or len(expected) != 32:
            return False
        derived = hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=parameters[0],
            r=parameters[1],
            p=parameters[2],
            dklen=32,
        )
        return hmac.compare_digest(derived.hex(), digest_hex)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(40)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def encrypt_secret(key: bytes, value: str) -> str:
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, value.encode(), None)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode()


def decrypt_secret(key: bytes, value: str) -> str:
    blob = base64.urlsafe_b64decode(value.encode())
    return AESGCM(key).decrypt(blob[:12], blob[12:], None).decode()
