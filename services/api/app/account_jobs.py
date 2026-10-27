"""个人账户远端任务与加密会话，协议模板仅复用不含账户身份的白名单。"""

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import hmac
import json
import uuid

from fastapi import HTTPException

from .auth import audit
from .security import decrypt_secret, encrypt_secret

PROTOCOL_KEYS = frozenset({"leyu_app_host", "leyu_app_signing_config", "leyu_app_discovery_config", "leyu_venue",
                           "leyu_login_proxy_url", "leyu_login_proxy_extract_url", "leyu_login_proxy_authkey",
                           "leyu_login_proxy_authpwd", "leyu_login_proxy_egress_url", "leyu_login_proxy_password_template"})
SESSION_KEYS = frozenset({"leyu_app_host", "leyu_api_base", "leyu_origin", "leyu_request_id", "leyu_app_token"})


def identity_hash(key: bytes, username: str) -> str:
    return hmac.new(key, ("leyu:2001:" + username.casefold()).encode(), hashlib.sha256).hexdigest()


def decode_document(key, value) -> dict:
    try:
        data = json.loads(decrypt_secret(key, value))
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except Exception:
        raise HTTPException(503, "个人账户配置解密失败，需核对密钥版本") from None


async def private_config(conn, account, settings) -> dict:
    credentials = decode_document(settings.encryption_key, account["encrypted_credentials"])
    if not all(credentials.get(key) for key in ("username", "password", "device_uuid")):
        raise HTTPException(409, "用户账户身份材料不完整，需重新绑定")
    rows = await conn.fetch("SELECT setting_key,encrypted_value FROM v2_admin_settings WHERE setting_key=ANY($1::text[])", list(PROTOCOL_KEYS))
    config = {}
    for row in rows:
        try:
            config[row["setting_key"]] = decrypt_secret(settings.encryption_key, row["encrypted_value"])
        except Exception:
            raise HTTPException(503, "账户协议模板解密失败") from None
    if not config.get("leyu_app_host") or not config.get("leyu_app_signing_config"):
        raise HTTPException(503, "用户账户登录协议模板尚未配置")
    if account["encrypted_session"]:
        document = decode_document(settings.encryption_key, account["encrypted_session"])
        config.update({key: value for key, value in document.items() if key in SESSION_KEYS})
    config.update(leyu_app_login_name=credentials["username"], leyu_app_login_password=credentials["password"], leyu_app_uuid=credentials["device_uuid"])
    return config


def scope(account) -> dict:
    return {"user_id": str(account["user_id"]), "account_id": str(account["id"]), "revision": account["config_revision"]}


async def enqueue(conn, account, kind: str):
    existing = await conn.fetchrow("SELECT id,state,kind FROM v2_account_jobs WHERE account_id=$1 AND state IN('queued','running')", account["id"])
    if existing:
        return existing
    return await conn.fetchrow("INSERT INTO v2_account_jobs(id,user_id,account_id,binding_revision,kind) VALUES($1,$2,$3,$4,$5) RETURNING id,state,kind",
                               uuid.uuid4(), account["user_id"], account["id"], account["binding_revision"], kind)


async def active_job(conn, job_id, attempt_id):
    job = await conn.fetchrow("SELECT * FROM v2_account_jobs WHERE id=$1", job_id)
    if not job:
        raise HTTPException(404, "账户任务不存在")
    account = await conn.fetchrow("SELECT * FROM v2_leyu_accounts WHERE id=$1 FOR UPDATE", job["account_id"])
    job = await conn.fetchrow("SELECT * FROM v2_account_jobs WHERE id=$1 FOR UPDATE", job_id)
    if (job["state"] != "running" or job["attempt_id"] != attempt_id or not job["lease_until"]
            or job["lease_until"] <= datetime.now(timezone.utc) or account["health_state"] == "disabled"
            or account["binding_revision"] != job["binding_revision"]):
        raise HTTPException(409, "账户任务租约或绑定版本已失效")
    return job, account


async def expire_leases(conn):
    # 运行中任务退出不能把可能提交过的资金订单重新放回队列。
    accounts = await conn.fetch("SELECT a.id FROM v2_leyu_accounts a WHERE (a.renewal_state='running' AND a.renewal_lease_until<=now()) "
        "OR EXISTS(SELECT 1 FROM v2_bet_orders o WHERE o.account_id=a.id AND o.state IN('pending','submitting') AND o.lease_until<=now()) "
        "OR EXISTS(SELECT 1 FROM v2_account_jobs j WHERE j.account_id=a.id AND j.state='running' AND j.lease_until<=now()) "
        "ORDER BY a.id FOR UPDATE OF a SKIP LOCKED LIMIT 50")
    for account in accounts:
        await conn.execute("UPDATE v2_bet_orders SET state='unknown',result_payload=jsonb_build_object('reason','executor_lease_expired'),updated_at=now() WHERE account_id=$1 AND state='submitting' AND lease_until<=now()", account['id'])
        await conn.execute("UPDATE v2_bet_orders SET attempt_id=NULL,lease_until=NULL,updated_at=now() WHERE account_id=$1 AND state='pending' AND lease_until<=now()", account['id'])
        await conn.execute("UPDATE v2_account_jobs SET state='unknown',last_error='executor_lease_expired',updated_at=now() WHERE account_id=$1 AND state='running' AND lease_until<=now()", account['id'])
        await conn.execute("UPDATE v2_leyu_accounts SET renewal_state='unknown',updated_at=now() WHERE id=$1 AND renewal_state='running' AND renewal_lease_until<=now()", account['id'])


async def reconcile_receipts(conn, account, receipts: list[dict]):
    for receipt in receipts:
        row = await conn.fetchrow("SELECT * FROM v2_bet_orders WHERE user_id=$1 AND provider_identity_hash=$2 AND provider_order_no=$3 "
                                  "AND state IN('accepted','unknown','submitting') FOR UPDATE", account["user_id"], account["provider_identity_hash"], receipt["order_no"])
        if not row:
            continue
        state = receipt.get("state")
        if state == "settled":
            await conn.execute("UPDATE v2_bet_orders SET state='settled',reserved_stake=0,settled_net=$2,settled_at=now(),result_payload=$3,updated_at=now() WHERE id=$1",
                               row["id"], Decimal(receipt['net_profit']) if receipt.get('net_profit') is not None else None, receipt)
        elif state == "unsettled":
            await conn.execute("UPDATE v2_bet_orders SET state='accepted',result_payload=$2,updated_at=now() WHERE id=$1", row["id"], receipt)
        else:
            continue
        await audit(conn, None, "executor", "order_reconciled", "bet_order", str(row["id"]), details={"state": state})
