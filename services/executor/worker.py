"""独立用户账户工作器；默认仅校验与查单，交易同时受API与本进程双开关限制。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
import os
import signal
import time

import httpx

from services.collector.leyu_protocol import AuthExpired, LeyuError
from .account_protocol import AccountProtocolClient, NativeSelection, SubmissionPermit, SubmissionRejected, SubmissionUnknown
from .account_session import AccountScope, AccountSessionManager, RenewalHooks

log = logging.getLogger("platform.executor")


class Executor:
    def __init__(self, api: httpx.AsyncClient, api_url: str, *, live_orders_enabled=False, client_factory=AccountProtocolClient,
                 session_factory=AccountSessionManager):
        self.api = api
        self.api_url = api_url.rstrip("/")
        self.live_orders_enabled = live_orders_enabled is True
        self.client_factory, self.session_factory = client_factory, session_factory

    async def rpc(self, method, path, **kwargs):
        try:
            response = await self.api.request(method, self.api_url + path, **kwargs)
            if response.status_code >= 300:
                raise LeyuError("executor_api_rejected", str(response.status_code))
            document = response.json()
            if not isinstance(document, dict):
                raise ValueError()
            return document
        except httpx.HTTPError:
            raise LeyuError("executor_api_unavailable") from None
        except ValueError:
            raise LeyuError("invalid_executor_api_response") from None

    async def finish(self, path, body):
        for attempt in range(3):
            try:
                return await self.rpc("POST", path, json=body)
            except LeyuError:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.2 * (attempt + 1))

    def manager(self, work):
        scope = AccountScope(**work["scope"])
        root = f"/jobs/{work['job_id']}"
        attempt_id = work["attempt_id"]
        renewal_attempt = ""

        async def claim(current, identity_hash):
            nonlocal renewal_attempt
            result = await self.rpc("POST", root + "/renewal/claim", json={"attempt_id": attempt_id,
                                    "expected_revision": current.revision, "identity_hash": identity_hash})
            renewal_attempt = result["attempt_id"]
            return renewal_attempt

        async def persist(current, expected, updates):
            result = await self.rpc("PUT", root + "/session", json={"attempt_id": attempt_id,
                                    "renewal_attempt": renewal_attempt, "expected_revision": expected, "updates": updates})
            return result["revision"]

        async def finish(current, renewal, status, metadata):
            await self.finish(root + "/renewal/finish", {"attempt_id": attempt_id, "renewal_attempt": renewal,
                                                        "status": status, "metadata": metadata})

        return self.session_factory(scope, work["config"], RenewalHooks(claim, persist, finish))

    async def account_job(self, work):
        root = f"/jobs/{work['job_id']}"
        result = {"attempt_id": work["attempt_id"], "status": "failed", "category": ""}
        try:
            async with asyncio.timeout(270):
                manager = self.manager(work)
                if not work["config"].get("leyu_request_id") or not work["config"].get("leyu_api_base"):
                    session = await manager.renew("authorized_bootstrap")
                else:
                    session = manager.venue_session()
                try:
                    async with self.client_factory(manager.scope, session) as client:
                        balance = await client.balance()
                except AuthExpired:
                    session = await manager.renew("session_expired")
                    async with self.client_factory(manager.scope, session) as client:
                        balance = await client.balance()
                receipts = []
                scanned = []
                async with self.client_factory(manager.scope, session) as client:
                    for order in work.get("orders", [])[:2]:
                        created = datetime.fromisoformat(order["created_at"].replace("Z", "+00:00"))
                        lookup = await client.lookup(order["provider_order_no"], begin=created - timedelta(minutes=5),
                                                     end=datetime.now(timezone.utc) + timedelta(minutes=5), max_pages=2)
                        scanned.append(order['id'])
                        if lookup["found"] and not lookup.get("ambiguous"):
                            item = lookup["receipt"]
                            receipts.append({key: item.get(key) for key in ("order_no", "state", "net_profit", "pnl_basis", "fees")})
                    if work.get('orders'):
                        balance = await client.balance()
                result.update(status="success", balance=balance["available"], observed_at=balance["observed_at"], receipts=receipts,scanned_orders=scanned)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            category = exc.category if isinstance(exc, LeyuError) else type(exc).__name__
            result.update(category=category, status="rejected" if category == "app_login_rejected" else "failed")
        await self.finish(root + "/finish", result)

    async def order_job(self, work):
        authorized = False
        result = {"attempt_id": work["attempt_id"], "state": "cancelled", "category": ""}
        try:
            if not self.live_orders_enabled:
                raise LeyuError("executor_live_orders_disabled")
            scope = AccountScope(**work["scope"])
            # 资金路径不进行内联续期；会话失效交给独立本人校验任务处理。
            manager = self.session_factory(scope, work["config"], RenewalHooks(None, None, None))
            async with self.client_factory(scope, manager.venue_session(), live_orders_enabled=True) as client:
                selected = NativeSelection(**work["selection"])
                prepared = await client.preflight(selected, Decimal(work["stake"]), device_imei=work["config"]["leyu_app_uuid"])
                permit = await self.rpc("POST", f"/orders/{work['order_id']}/authorize", json={"attempt_id": work["attempt_id"],
                                        "prepared_digest": prepared.digest, "payload_json": prepared.payload_json})
                authorized = True
                permit["scope"] = AccountScope(**permit["scope"])
                receipt = await client.submit_once(prepared, SubmissionPermit(**permit))
                result.update(state=receipt["state"], order_no=receipt["order_no"], provider_code=receipt["provider_code"])
        except asyncio.CancelledError:
            raise
        except SubmissionRejected as exc:
            result.update(state="rejected", category=exc.category, provider_code=exc.code)
        except Exception as exc:
            result.update(state="unknown" if authorized else "cancelled", category=exc.category if isinstance(exc, LeyuError) else type(exc).__name__)
        await self.finish(f"/orders/{work['order_id']}/finish", result)

    async def run(self, stop):
        failures, dispatch_at = 0, 0.0
        while not stop.is_set():
            try:
                if time.monotonic() >= dispatch_at:
                    await self.rpc("POST", "/dispatch")
                    dispatch_at = time.monotonic() + 10
                work = await self.rpc("POST", "/claim", params={"live_capable": str(self.live_orders_enabled).lower()})
                if work.get("kind") == "account":
                    await self.account_job(work)
                elif work.get("kind") == "order":
                    await self.order_job(work)
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                log.warning("executor unavailable category=%s", exc.category if isinstance(exc, LeyuError) else type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=2 if not failures else min(60, 2 ** min(failures, 6)))
            except asyncio.TimeoutError:
                pass


async def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    token = os.environ.get("EXECUTOR_INTERNAL_TOKEN", "")
    if not token:
        raise RuntimeError("EXECUTOR_INTERNAL_TOKEN is required")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    async with httpx.AsyncClient(timeout=httpx.Timeout(25, connect=3), trust_env=False, follow_redirects=False,
                                 headers={"Authorization": "Bearer " + token}) as api:
        executor = Executor(api, os.environ.get("PLATFORM_EXECUTOR_API", "http://api:8080/internal/executor"),
                            live_orders_enabled=os.environ.get("EXECUTOR_LIVE_ORDERS_ENABLED", "false").casefold() == "true")
        task = asyncio.create_task(executor.run(stop))
        await stop.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
