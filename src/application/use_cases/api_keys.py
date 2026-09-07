"""Issue, list, and revoke the keys other platforms authenticate with.

The rule this module exists to enforce: **a key can only ever carry scopes its
plan grants.** Everything else here is bookkeeping around that one check.

Three consequences, each deliberate:

  * The raw key is returned exactly once, from `CreateApiKey`. We store a
    SHA-256 of it and nothing else, so a lost key is reissued, never recovered.
    (`hash_api_key` explains why SHA-256 and not Argon2 for this input.)
  * The free tier is refused at creation, with an error that names the cheapest
    plan that would work. "Upgrade to continue" with no destination is the
    single most annoying thing a paywall can say.
  * Asking for a scope above your tier is a 403 naming the scope, not a silent
    downgrade to the scopes you *do* have. A key that quietly came back with
    fewer permissions than requested would fail later, in the caller's
    production, with a 403 on an endpoint they thought they had bought.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass

from src.application.ports.repositories import UnitOfWork
from src.application.use_cases.billing import load_subscription
from src.domain.billing.entities import ApiScope, PlanTier, plan_for
from src.domain.shared.errors import NotFoundError, PermissionDeniedError
from src.domain.shared.identifiers import TenantId
from src.domain.tenant.entities import ApiKey
from src.infrastructure.security.hashing import hash_api_key

# Pairs with the widget's publishable `pk_` (see chatbot entities): `sk_` is the
# secret half — server-side only, full tenant scope, never in page source.
SECRET_KEY_PREFIX = "sk_"
# How much of the key we keep in the clear. Enough to tell two keys apart in a
# list without being enough to help anyone guess the rest.
_PREFIX_CHARS = 12


def generate_secret_key() -> str:
    return f"{SECRET_KEY_PREFIX}{secrets.token_urlsafe(32)}"


@dataclass(frozen=True)
class IssuedApiKey:
    """A newly minted key and its one and only appearance in plaintext."""

    key: ApiKey
    raw_key: str


class CreateApiKey:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        tenant_id: TenantId,
        *,
        name: str,
        requested_scopes: list[str] | None = None,
    ) -> IssuedApiKey:
        async with self._uow as uow:
            uow.set_tenant_scope(tenant_id)
            subscription = await load_subscription(uow, tenant_id)
            # The *effective* plan: a lapsed Scale subscription mints Free-tier
            # keys, which is to say none.
            plan = subscription.plan

            if not plan.api_access:
                raise PermissionDeniedError(
                    "API keys are available on paid plans. Upgrade to Starter "
                    "($5/mo) for the management API, or Scale ($15/mo) to run "
                    "assistants as agents over the API."
                )

            granted = _resolve_scopes(requested_scopes, plan.scopes)

            raw = generate_secret_key()
            key = ApiKey(
                tenant_id=tenant_id,
                name=name.strip() or "API key",
                key_hash=hash_api_key(raw),
                prefix=raw[:_PREFIX_CHARS],
                scopes=sorted(s.value for s in granted),
                plan_tier=plan.tier.value,
            )
            await uow.api_keys.add(key)
            await uow.commit()

        return IssuedApiKey(key=key, raw_key=raw)


def _resolve_scopes(
    requested: list[str] | None, allowed: frozenset[ApiScope]
) -> frozenset[ApiScope]:
    """Which scopes the new key gets.

    No request = everything the plan allows, which is what someone clicking
    "Create key" means. An explicit request is honoured exactly, or refused —
    see the module docstring on why we never quietly narrow it.
    """
    if not requested:
        return allowed

    resolved: set[ApiScope] = set()
    for raw in requested:
        try:
            scope = ApiScope(raw)
        except ValueError as exc:
            raise PermissionDeniedError(f"Unknown scope: {raw}") from exc
        if scope not in allowed:
            raise PermissionDeniedError(
                f"The {scope.value} scope is not included in your plan."
                + (
                    " Agent access over the API is part of the Scale plan ($15/mo)."
                    if scope is ApiScope.AGENT_RUN
                    else ""
                )
            )
        resolved.add(scope)
    return frozenset(resolved)


class ListApiKeys:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, tenant_id: TenantId) -> tuple[list[ApiKey], PlanTier]:
        """The tenant's keys, with the tier they are judged against today.

        Both, because the dashboard has to be able to show a key minted under
        Scale as no longer carrying `agent.run` after a downgrade — the key's
        own `scopes` column still says it does, and the live plan is what
        settles it (`deps.py` does the same intersection on the request path).
        """
        async with self._uow as uow:
            uow.set_tenant_scope(tenant_id)
            keys = await uow.api_keys.list_for_tenant(tenant_id)
            subscription = await load_subscription(uow, tenant_id)
            return keys, subscription.effective_tier


class RevokeApiKey:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, tenant_id: TenantId, key_id: uuid.UUID) -> None:
        async with self._uow as uow:
            uow.set_tenant_scope(tenant_id)
            if not await uow.api_keys.revoke(tenant_id, key_id):
                raise NotFoundError("API key not found.")
            await uow.commit()


def effective_scopes(key: ApiKey, tier: PlanTier | str) -> frozenset[ApiScope]:
    """What the key can do *now*: its stamped scopes ∩ the live plan's.

    The intersection is the whole security model for downgrades. A key minted on
    Scale keeps `agent.run` in its own row forever; the moment the workspace
    drops to Starter, the plan stops granting it and this returns without it —
    no sweep, no migration, no window where a stale key still works.
    """
    plan = plan_for(tier)
    stamped: set[ApiScope] = set()
    for raw in key.scopes:
        try:
            stamped.add(ApiScope(raw))
        except ValueError:
            continue  # a scope this build no longer knows grants nothing
    return frozenset(stamped & plan.scopes)
