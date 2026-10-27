"""显式用户作用域会话与共享App续期；禁止读取采集账号或环境凭据。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Mapping
from uuid import UUID

from services.collector.leyu import safe_base_url
from services.collector.leyu_protocol import LeyuError
from services.collector.leyu_session import AppSessionRenewer


@dataclass(frozen=True, repr=False)
class AccountScope:
    user_id: str
    account_id: str
    revision: int

    def __post_init__(self):
        try:
            UUID(self.user_id)
            UUID(self.account_id)
        except (ValueError, TypeError, AttributeError):
            raise LeyuError("invalid_account_scope") from None
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise LeyuError("invalid_account_revision")


@dataclass(frozen=True, repr=False)
class VenueSession:
    scope: AccountScope
    base_url: str
    request_id: str
    origin: str

    def __post_init__(self):
        safe_base_url(self.base_url)
        safe_base_url(self.origin)
        if not self.request_id or len(self.request_id) > 8192 or any(char in self.request_id for char in "\r\n"):
            raise LeyuError("authorized_account_session_missing")


@dataclass(frozen=True, repr=False)
class RenewalHooks:
    claim: Callable[[AccountScope, str], Awaitable[str]]
    persist: Callable[[AccountScope, int, dict[str, str]], Awaitable[int]]
    finish: Callable[[AccountScope, str, str, dict[str, Any]], Awaitable[None]]


ACCOUNT_CONFIG_KEYS = frozenset({
    "leyu_api_base", "leyu_origin", "leyu_request_id", "leyu_app_host", "leyu_app_token", "leyu_app_uuid",
    "leyu_app_login_name", "leyu_app_login_password", "leyu_app_signature", "leyu_app_login_signature",
    "leyu_app_signing_config", "leyu_app_discovery_config", "leyu_venue", "leyu_login_proxy_url",
    "leyu_login_proxy_password_template",
})
ACCOUNT_PERSIST_KEYS = frozenset({"leyu_api_base", "leyu_origin", "leyu_request_id", "leyu_app_host", "leyu_app_token"})


class AccountSessionManager:
    def __init__(self, scope: AccountScope, private_config: Mapping[str, Any], hooks: RenewalHooks, **renewal_options):
        if set(private_config) - ACCOUNT_CONFIG_KEYS:
            raise LeyuError("unapproved_account_configuration")
        self.scope = scope
        self._config = dict(private_config)
        self.hooks = hooks
        self.renewal_options = renewal_options

    def venue_session(self) -> VenueSession:
        return VenueSession(self.scope, str(self._config.get("leyu_api_base") or ""),
                            str(self._config.get("leyu_request_id") or ""),
                            str(self._config.get("leyu_origin") or self._config.get("leyu_api_base") or ""))

    async def renew(self, reason: str) -> VenueSession:
        # hooks 必须由用户账户API提供；绝不调用采集账号的全局续期端点。
        async def claim(identity_hash: str) -> str:
            return await self.hooks.claim(self.scope, identity_hash)

        async def persist(updates: dict[str, str]) -> None:
            if not updates or set(updates) - ACCOUNT_PERSIST_KEYS:
                raise LeyuError("unapproved_account_renewal_update")
            revision = await self.hooks.persist(self.scope, self.scope.revision, updates)
            if isinstance(revision, bool) or not isinstance(revision, int) or revision <= self.scope.revision:
                raise LeyuError("invalid_account_revision_receipt")
            self._config.update(updates)
            self.scope = replace(self.scope, revision=revision)

        async def finish(attempt: str, status: str, metadata: dict[str, Any]) -> None:
            await self.hooks.finish(self.scope, attempt, status, metadata)

        renewer = AppSessionRenewer(self._config, claim=claim, persist=persist, finish=finish, **self.renewal_options)
        await renewer.renew(reason)
        return self.venue_session()
