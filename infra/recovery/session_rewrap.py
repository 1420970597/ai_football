"""Explicit offline Telegram session rewrap; never reads or generates a key."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re

from cryptography.fernet import Fernet, InvalidToken
from telethon.sessions import StringSession

from services.api.app.security import decrypt_secret, encrypt_secret
from .bundle import RecoveryError


SOURCE_ALGORITHM = 'fernet'
TARGET_ALGORITHM = 'aes-256-gcm'
MAX_CIPHERTEXT_BYTES = 8192


def _reference(value):
    if not isinstance(value, str) or not re.fullmatch(r'keyref:[a-zA-Z0-9][a-zA-Z0-9_.:/@-]{2,140}', value):
        raise RecoveryError('session_key_reference_invalid')
    return value


def _canonical_session(value):
    try:
        if not value or len(value) > 4096 or value != value.strip():
            raise ValueError()
        session = StringSession(value)
        if (not session.auth_key or not session.dc_id or not session.server_address
                or not session.port or session.save() != value):
            raise ValueError()
    except Exception:
        raise RecoveryError('legacy_session_structure_invalid') from None
    return value


def rewrap_session(ciphertext: bytes, *, old_key: bytes, new_key: bytes, source_algorithm: str,
                   source_version: int, target_version: int, source_key_reference: str,
                   target_key_reference: str, expected_session: str | None = None) -> dict:
    """Return an encrypted envelope compatible with current decrypt_secret.

    Legacy Fernet has no application version header. source_version=1 is the
    caller's audited legacy contract, not an invented claim about its token.
    An optional separately obtained session is compared only as a boolean.
    """
    if (source_algorithm != SOURCE_ALGORITHM or type(source_version) is not int or source_version != 1
            or type(target_version) is not int or target_version != 1):
        raise RecoveryError('session_rewrap_algorithm_or_version_unsupported')
    source_ref, target_ref = _reference(source_key_reference), _reference(target_key_reference)
    if source_ref == target_ref:
        raise RecoveryError('independent_source_and_target_key_references_required')
    if not isinstance(old_key, bytes) or not isinstance(new_key, bytes) or len(new_key) != 32:
        raise RecoveryError('session_rewrap_explicit_keys_invalid')
    try:
        raw_old_key = base64.b64decode(old_key, altchars=b'-_', validate=True)
        if len(raw_old_key) != 32:
            raise ValueError()
        cipher = Fernet(old_key)
    except (ValueError, TypeError):
        raise RecoveryError('session_rewrap_explicit_keys_invalid') from None
    if hmac.compare_digest(raw_old_key, new_key):
        raise RecoveryError('independent_source_and_target_keys_required')
    if not isinstance(ciphertext, bytes) or not 1 <= len(ciphertext) <= MAX_CIPHERTEXT_BYTES:
        raise RecoveryError('legacy_session_ciphertext_budget_invalid')
    try:
        plain = cipher.decrypt(ciphertext).decode('utf-8')
    except (InvalidToken, UnicodeDecodeError, ValueError):
        raise RecoveryError('legacy_session_decryption_failed') from None
    canonical = _canonical_session(plain)
    matches = None
    if expected_session is not None:
        expected = _canonical_session(expected_session)
        matches = hmac.compare_digest(canonical.encode(), expected.encode())
        if not matches:
            raise RecoveryError('legacy_session_source_comparison_failed')
    encrypted = encrypt_secret(new_key, canonical)
    if not hmac.compare_digest(decrypt_secret(new_key, encrypted).encode(), canonical.encode()):
        raise RecoveryError('rewrapped_session_roundtrip_failed')
    return {'format': 'telegram-session-rewrap-1',
            'source': {'algorithm': SOURCE_ALGORITHM, 'contract_version': 1, 'key_reference': source_ref,
                       'ciphertext_sha256': hashlib.sha256(ciphertext).hexdigest()},
            'target': {'algorithm': TARGET_ALGORITHM, 'contract_version': 1, 'key_reference': target_ref,
                       'ciphertext': encrypted, 'layout': 'base64url(nonce12+ciphertext+tag16);aad=None'},
            'validation': {'string_session_canonical': True, 'target_roundtrip_verified': True,
                           'separate_source_matches': matches, 'telegram_authorization_verified': False},
            'keys_generated': False, 'files_written': False, 'live_session_replaced': False}
