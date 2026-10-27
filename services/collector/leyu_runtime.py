"""将协议客户端与远端配置、持久续期保护连接；所有持久变更通过鉴权 API。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

import httpx

from .leyu import LeyuClient, safe_base_url
from .leyu_protocol import AuthExpired, LeyuError
from .leyu_session import AppSessionRenewer, LoginProxy

LOCAL_DEFAULT_KEYS = frozenset({"leyu_app_signing_config", "leyu_app_discovery_config", "leyu_user_agent",
                                "leyu_batch_size", "leyu_batch_concurrency", "leyu_batch_interval_seconds"})

class LeyuRuntime:
    def __init__(self, api: httpx.AsyncClient, api_url: str, base_config: Mapping[str, Any] | None = None):
        self.api = api
        self.api_url = api_url.rstrip("/")
        # 远端删除/撤销的授权不能被旧本地凭据兜底复活；本地仅保留挂载引用及资源默认值。
        self.base_config = {key: value for key, value in (base_config or {}).items() if key in LOCAL_DEFAULT_KEYS}
        self.config: dict[str, Any] = {}
        self.version = 0
        self.config_validated_at: str | None = None
        self.config_validation_error = ""
        self.attempt_id = ""
        self.last_report: dict[str, Any] = {}
        self.fixtures: dict[str, dict[str, Any]] = {}
        self._collection_lock = asyncio.Lock()

    async def _rpc(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        try:
            response = await self.api.request(method, self.api_url + "/collector/renewal" + path, **kwargs)
            if response.status_code >= 300:
                status = response.status_code
                if status == 409:
                    category = "renewal_state_conflict"
                elif status in {401, 403}:
                    category = "renewal_api_unauthorized"
                elif status == 422:
                    category = "renewal_request_invalid"
                elif status >= 500:
                    category = "renewal_api_unavailable"
                else:
                    category = "renewal_api_rejected"
                raise LeyuError(category, str(status))
            value = response.json()
            if not isinstance(value, dict):
                raise LeyuError("invalid_renewal_api_response")
            return value
        except httpx.TimeoutException:
            raise LeyuError("renewal_api_timeout") from None
        except httpx.HTTPError:
            raise LeyuError("renewal_api_transport_failure") from None
        except ValueError:
            raise LeyuError("invalid_renewal_api_response") from None

    async def load(self) -> dict[str, Any]:
        self.config_validation_error = ""
        document = await self._rpc("GET", "/config")
        if not isinstance(document.get("items"), dict) or not isinstance(document.get("version"), int) or document["version"] < 1:
            raise LeyuError("invalid_renewal_config")
        candidate = {**self.base_config, **document["items"]}
        try:
            self._validate_candidate(candidate)
        except LeyuError as exc:
            self.config_validation_error = exc.category
            raise
        # The revision is the runtime acknowledgement only after the complete
        # candidate validates. Keep the previous working session on failure.
        self.config = candidate
        self.version = document["version"]
        self.config_validated_at = datetime.now(timezone.utc).isoformat()
        self.config_validation_error = ""
        return self.config

    @staticmethod
    def _validate_candidate(config: Mapping[str, Any]) -> None:
        LeyuClient.validate_collection_budget(config)
        if str(config.get("leyu_collection_relay") or "").strip():
            safe_base_url(str(config["leyu_collection_relay"]).strip())

        request_id = str(config.get("leyu_request_id") or "").strip()
        business_base = str(config.get("leyu_api_base") or "").strip()
        bootstrap_keys = ("leyu_app_host", "leyu_app_uuid", "leyu_app_login_name", "leyu_app_token",
                          "leyu_api_token", "leyu_app_login_password")
        bootstrap = any(str(config.get(key) or "").strip() for key in bootstrap_keys)
        if request_id:
            LeyuClient.validate_config(config)
            LoginProxy(config).validate_configuration()
            if str(config.get("leyu_app_host") or "").strip():
                LeyuRuntime._validate_app_renewal(config)
        elif bootstrap:
            if not str(config.get("leyu_app_host") or "").strip():
                raise LeyuError("app_host_missing")
            LeyuRuntime._validate_app_renewal(config)
        elif business_base:
            raise LeyuError("authorized_session_missing")

    @staticmethod
    def _validate_app_renewal(config: Mapping[str, Any]) -> None:
        safe_base_url(str(config.get("leyu_app_host") or "").strip())
        if not str(config.get("leyu_app_uuid") or "").strip():
            raise LeyuError("app_identity_missing")
        app_token = str(config.get("leyu_app_token") or config.get("leyu_api_token") or "").strip()
        if not app_token and not (str(config.get("leyu_app_login_name") or "").strip()
                                  and str(config.get("leyu_app_login_password") or "").strip()):
            raise LeyuError("app_login_credentials_missing")

        async def unused(*_args):
            return ""

        # Validate mounted documents, signatures, and endpoint shape locally;
        # this does not probe the provider or submit credentials.
        renewer = AppSessionRenewer(config, claim=unused, finish=unused, persist=unused, verify_public_hosts=False)
        renewer._headers("/site/api", "192.168.28.123")
        renewer._headers("/game/api", "192.168.28.123", "validation-only")
        LoginProxy(config).validate_configuration()

    async def _claim(self, account_hash: str) -> str:
        result = await self._rpc("POST", "/claim", json={"account_hash": account_hash, "expected_version": self.version})
        self.attempt_id = str(result.get("attempt_id") or "")
        return self.attempt_id

    async def _finish(self, attempt: str, status: str, metadata: dict[str, Any]) -> None:
        await self._rpc("POST", f"/{attempt}/finish", json={"status": status, "metadata": metadata})

    async def _persist(self, updates: dict[str, str]) -> None:
        candidate = {**self.config, **updates}
        self._validate_candidate(candidate)
        result = await self._rpc("PUT", "/config", json={"attempt_id": self.attempt_id,
                                 "expected_version": self.version, "values": updates})
        revision = result.get("version")
        if type(revision) is not int or revision <= self.version:
            raise LeyuError("invalid_renewal_config_revision")
        self.config = candidate
        self.version = revision
        self.config_validated_at = datetime.now(timezone.utc).isoformat()
        self.config_validation_error = ""

    async def collect(self, *, odds_mids: Iterable[str] | Callable[[list[dict[str, Any]]], Iterable[str]] | None = None) -> list[dict[str, Any]]:
        async with self._collection_lock:
            return await self._collect(odds_mids=odds_mids)

    async def _collect(self, *, odds_mids: Iterable[str] | Callable[[list[dict[str, Any]]], Iterable[str]] | None = None) -> list[dict[str, Any]]:
        try:
            await self.load()
        except LeyuError:
            if not (self.config_validation_error and self.version > 0 and self.config):
                raise
        try:
            if (not str(self.config.get("leyu_request_id") or "").strip() and self.config.get("leyu_app_host")
                    and (self.config.get("leyu_app_login_name") or self.config.get("leyu_app_uuid"))):
                # 仅配置了被授权App身份时，业务网关尚未创建是正常初始化状态。
                # 不把一般的错误URL或403放宽为登录触发条件。
                raise LeyuError("authorized_session_missing")
            client = LeyuClient.from_config(self.config)
            result = await client.fetch(odds_mids=odds_mids)
        except LeyuError as exc:
            if not isinstance(exc, AuthExpired) and exc.category != "authorized_session_missing":
                raise
            renewer = AppSessionRenewer(self.config, claim=self._claim, finish=self._finish, persist=self._persist)
            await renewer.renew("session_expired" if isinstance(exc, AuthExpired) else "authorized_bootstrap")
            # 一次受控续期后只重试一次，不无限登录循环。
            client = LeyuClient.from_config(self.config)
            result = await client.fetch(odds_mids=odds_mids)
        self.last_report = {**client.last_report, "config_version": self.version}
        previous_fixtures = self.fixtures
        for fixture in result:
            previous = previous_fixtures.get(fixture["id"])
            if not previous or fixture.get("markets_complete"):
                continue
            markets = {(quote.get("market_id"), quote.get("period"), quote.get("line"), quote.get("selection")): quote
                       for quote in previous.get("markets", [])}
            for quote in fixture.get("markets", []):
                key = (quote.get("market_id"), quote.get("period"), quote.get("line"), quote.get("selection"))
                current = markets.get(key)
                if current and current.get("source_updated_at") and quote.get("source_updated_at"):
                    if quote["source_updated_at"] < current["source_updated_at"]:
                        continue
                markets[key] = {**(current or {}), **quote}
            fixture["markets"] = list(markets.values())
            fixture["markets_complete"] = False
        self.fixtures = {fixture["id"]: deepcopy(fixture) for fixture in result}
        return result

    def apply_push(self, events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        """将已核验增量附于可丢弃全量投影；未知状态/比赛要求补同步。"""
        affected: dict[str, dict[str, Any]] = {}
        resync = False
        for event in events:
            identity = str(event.get("fixture_id") or "")
            current = self.fixtures.get(identity)
            if current is None:
                resync = True
                continue
            resync |= bool(event.get("requires_resync"))
            source_time = event.get("source_updated_at")
            old_source_time = current.get("source_updated_at")
            patch = event.get("fixture_patch") or {}
            if patch and (not old_source_time or (source_time and source_time >= old_source_time)):
                current.update(patch)
                current.update(source_updated_at=source_time, source_time_quality="known" if source_time else "unknown")
            changed = []
            indexed = {self._market_key(quote): quote for quote in current.get("markets", [])}
            for quote in event.get("markets", []):
                key = self._market_key(quote)
                previous = indexed.get(key)
                quote_time = quote.get("source_updated_at")
                if not quote_time:
                    resync = True
                    continue
                if previous and previous.get("source_updated_at") and quote_time < previous["source_updated_at"]:
                    continue
                merged = {**(previous or {}), **quote}
                if not quote.get("status_evidence"):
                    if (not previous or previous.get("market_instance_id") != quote.get("market_instance_id")
                            or previous.get("selection_id") != quote.get("selection_id")):
                        resync = True
                        continue
                    # 报价变化本身不证明复盘；沿用已有状态，未知/无效价格仍停盘。
                    merged["status"] = previous["status"] if quote.get("price") else "suspended"
                merged.pop("status_evidence", None)
                indexed[key] = merged
                changed.append(merged)
            current["markets"] = list(indexed.values())
            current["received_at"] = event.get("received_at") or current.get("received_at")
            if changed or patch:
                output = affected.setdefault(identity, {**deepcopy(current), "markets": [], "markets_complete": False})
                output.update({key: value for key, value in current.items() if key != "markets"})
                output["markets_complete"] = False
                output["markets"].extend(deepcopy(changed))
        return list(affected.values()), resync

    @staticmethod
    def _market_key(quote: Mapping[str, Any]) -> tuple:
        return quote.get("market_id"), quote.get("period"), quote.get("line"), quote.get("selection")
