"""Validated non-secret runtime settings, read per operation across workers."""

from __future__ import annotations

import hashlib
import json
import re

from fastapi import HTTPException

from . import database
from .security import decrypt_secret

RUNTIME_KEYS = frozenset({"hermes_analysis_model", "quote_max_age_seconds"})
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")


def validate_runtime_value(key: str, value: str) -> str:
    text = str(value).strip()
    if key == "quote_max_age_seconds":
        if not text.isascii() or not text.isdecimal() or not 1 <= int(text) <= 3600:
            raise ValueError("报价有效秒数必须为1至3600的整数")
        return str(int(text))
    if key == "hermes_analysis_model":
        if text and (not MODEL_ID.fullmatch(text) or "://" in text or text == "hermes-agent"):
            raise ValueError("请输入有效模型标识，留空使用Hermes默认模型")
        return text
    raise ValueError("不支持的运行配置")


async def runtime_configuration(settings, connection=None) -> dict:
    connection = connection or database.current_pool()
    rows = await connection.fetch(
        "SELECT setting_key,encrypted_value,updated_at FROM v2_admin_settings "
        "WHERE setting_key=ANY($1::text[])", sorted(RUNTIME_KEYS | {'_model_configuration_v1'}),
    )
    values = {}
    governance = None
    for row in rows:
        if row['setting_key'] == '_model_configuration_v1':
            from .model_configuration import decode_state
            governance = decode_state(settings, row['encrypted_value'])
            continue
        try:
            raw = decrypt_secret(settings.encryption_key, row["encrypted_value"])
            values[row["setting_key"]] = validate_runtime_value(row["setting_key"], raw)
        except Exception:
            raise HTTPException(503, detail="运行配置不可读取，请核对值和密钥版本") from None
    quote_age = int(values.get("quote_max_age_seconds", getattr(settings, "quote_max_age_seconds", 15)))
    from .model_configuration import legacy_state
    state = governance or legacy_state(settings, values.get('hermes_analysis_model'))
    snapshot = {"model": state['active']['model'],
                "quote_max_age_seconds": quote_age}
    version = hashlib.sha256(json.dumps({**snapshot, 'model_version': state['active']['version']}, sort_keys=True).encode()).hexdigest()
    return {**snapshot, "configuration_version": version, 'model_configuration': {
        'version': state['active']['version'], 'revision': state['active']['revision'],
        'provider': state['active']['provider'], 'provenance': state['active']['provenance'],
        'origin': state['active']['origin']}}


async def runtime_quote_max_age(settings, connection=None) -> int:
    connection = connection or database.current_pool()
    rows = await connection.fetch("SELECT setting_key,encrypted_value FROM v2_admin_settings WHERE setting_key=ANY($1::text[])", ['quote_max_age_seconds'])
    for row in rows:
        if row['setting_key'] != 'quote_max_age_seconds':
            continue
        try:
            return int(validate_runtime_value('quote_max_age_seconds', decrypt_secret(settings.encryption_key, row['encrypted_value'])))
        except Exception:
            raise HTTPException(503, '运行配置不可读取，请核对值和密钥版本') from None
    return int(getattr(settings, 'quote_max_age_seconds', 15))
