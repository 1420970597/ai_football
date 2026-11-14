"""Explicit projection maintenance inside the existing key-owning API only."""
import hashlib

from .runtime_settings import validate_runtime_value
from .security import decrypt_secret


async def sync_quote_policy(conn, settings) -> dict:
    """Caller owns the transaction; never export the key or ciphertext."""
    cipher = await conn.fetchval("SELECT encrypted_value FROM public.v2_admin_settings "
                                "WHERE setting_key='quote_max_age_seconds' FOR UPDATE")
    if cipher is None:
        await conn.execute("DELETE FROM public.v2_review_quote_policy WHERE setting_key='quote_max_age_seconds'")
        return {'custom_quote_policy': False}
    value = int(validate_runtime_value('quote_max_age_seconds', decrypt_secret(settings.encryption_key, cipher)))
    await conn.execute("INSERT INTO public.v2_review_quote_policy(setting_key,max_age_seconds,source_cipher_sha256) "
                       "VALUES('quote_max_age_seconds',$1,$2) ON CONFLICT(setting_key) DO UPDATE "
                       'SET max_age_seconds=EXCLUDED.max_age_seconds,source_cipher_sha256=EXCLUDED.source_cipher_sha256,updated_at=now()',
                       value, hashlib.sha256(cipher.encode()).hexdigest())
    return {'custom_quote_policy': True, 'quote_max_age_seconds': value}
