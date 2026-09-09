"""Request-scoped dependencies: container access and the authenticated principal.

Authentication accepts either a Bearer JWT (user sessions) or an `X-API-Key`
header (programmatic). Both resolve to a `Principal` carrying the tenant scope —
the tenant id is never read from the request body.

The two paths are not equivalent, and the difference is the point of the paid
API. A JWT is a person in the dashboard: they see what their plan gives them,
enforced where the limit lives (assistant creation checks the assistant
ceiling, and so on). An API key is another platform calling in on the
customer's behalf, so it carries *scopes*, is metered against the plan's
monthly allowance, and is re-checked against the live plan on every request —
see `effective_scopes` for why that last part matters more than it looks.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from fastapi import Depends, Header, HTTPException

from src.application.use_cases.api_keys import effective_scopes
from src.application.use_cases.billing import load_subscription, period_start
from src.config.container import Container, get_container
from src.domain.billing.entities import ApiScope
from src.domain.shared.identifiers import TenantId, UserId
from src.infrastructure.security.hashing import hash_api_key


@dataclass
class Principal:
    tenant_id: TenantId
    user_id: UserId | None
    role: str
    # How this request authenticated. Scope checks apply to `api_key` only: a
    # signed-in person is not holding a token with a subset of permissions,
    # they are the account.
    auth: Literal["jwt", "api_key"] = "jwt"
    # Known on the API-key path, where we had to load the subscription anyway.
    plan_tier: str | None = None
    scopes: frozenset[ApiScope] = field(default_factory=frozenset)
    api_key_id: uuid.UUID | None = None
    # Platform-wide, orthogonal to `role` (which is tenant-scoped). Never true
    # for an API key — see `_principal_from_api_key`.
    is_platform_admin: bool = False

    def has_scope(self, scope: ApiScope) -> bool:
        return self.auth != "api_key" or scope in self.scopes


def container_dep() -> Container:
    return get_container()


ContainerDep = Annotated[Container, Depends(container_dep)]


async def current_principal(
    container: ContainerDep,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> Principal:
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1]
        try:
            claims = container.tokens.decode(token)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=401, detail="Invalid or expired token") from exc
        if claims.get("type") != "access":
            raise HTTPException(status_code=401, detail="Wrong token type")
        return Principal(
            tenant_id=TenantId(uuid.UUID(claims["tenant_id"])),
            user_id=UserId(uuid.UUID(claims["sub"])),
            role=claims.get("role", "member"),
            is_platform_admin=bool(claims.get("is_platform_admin", False)),
        )

    if x_api_key:
        return await _principal_from_api_key(container, x_api_key)

    raise HTTPException(status_code=401, detail="Authentication required")


async def _principal_from_api_key(container: Container, raw_key: str) -> Principal:
    """Authenticate a key, then decide what its plan still lets it do.

    All of it in one transaction: the lookup, the plan read, the allowance
    check, and the two counters we write. Splitting the write off into a
    background task was tempting and wrong — usage that is not durable is a
    meter customers can outrun by sending requests faster than we flush.
    """
    async with container.unit_of_work() as uow:
        key = await uow.api_keys.get_by_hash(hash_api_key(raw_key))
        if key is None:
            # One message for "no such key" and "revoked key" alike: which of
            # the two it is tells an attacker whether they have found a real
            # key, and tells a legitimate caller nothing they can act on.
            raise HTTPException(status_code=401, detail="Invalid API key")

        uow.set_tenant_scope(key.tenant_id)
        subscription = await load_subscription(uow, key.tenant_id)
        plan = subscription.plan
        scopes = effective_scopes(key, subscription.effective_tier)

        if not scopes:
            # The plan that issued this key no longer grants anything — a lapsed
            # or downgraded subscription. Say so plainly; this one *is*
            # actionable, and the caller is the account holder.
            raise HTTPException(
                status_code=403,
                detail=(
                    "This API key's plan no longer includes API access. "
                    "Renew or upgrade the workspace's plan to continue."
                ),
            )

        used = await uow.api_usage.calls_since(key.tenant_id, period_start(subscription).date())
        if used >= plan.monthly_api_calls:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Monthly API allowance of {plan.monthly_api_calls:,} calls "
                    f"reached for the {plan.name} plan."
                ),
            )

        await uow.api_usage.record_call(key.tenant_id)
        await uow.api_keys.touch(key.id)
        await uow.commit()

    return Principal(
        tenant_id=key.tenant_id,
        user_id=None,
        # API keys act for the workspace, not a person. `member` keeps them out
        # of the admin surfaces (team management, billing changes) — an
        # integration should never be able to change what the customer pays.
        role="member",
        auth="api_key",
        plan_tier=subscription.effective_tier.value,
        scopes=scopes,
        api_key_id=key.id,
    )


PrincipalDep = Annotated[Principal, Depends(current_principal)]


_MANAGE_ROLES = ("owner", "admin")


async def require_admin(principal: PrincipalDep) -> Principal:
    """Gate for the "admin panel" surfaces (interviews, bulk invites, hiring
    agent, channels, team management) — only an Owner or Admin may proceed.
    Mirrors `User.can_manage()` (`src/domain/tenant/entities.py`), applied at
    the API boundary since `Principal` (built from JWT claims / API key) is
    what routers actually see, not the `User` row itself."""
    if principal.role not in _MANAGE_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return principal


AdminPrincipalDep = Annotated[Principal, Depends(require_admin)]


async def require_platform_admin(principal: PrincipalDep) -> Principal:
    """Gate for the cross-tenant Super Admin panel. Independent of `role` —
    a platform admin may hold any (or no) tenant role in their own workspace."""
    if not principal.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin access required.")
    return principal


PlatformAdminPrincipalDep = Annotated[Principal, Depends(require_platform_admin)]


def require_scope(
    scope: ApiScope,
) -> Callable[[Principal], Coroutine[Any, Any, Principal]]:
    """Dependency factory gating one endpoint on one scope.

    A no-op for signed-in people (see `Principal.has_scope`), so adding it to an
    endpoint the dashboard also calls is safe — it constrains integrations only.
    """

    async def _dependency(principal: PrincipalDep) -> Principal:
        if not principal.has_scope(scope):
            raise HTTPException(
                status_code=403,
                detail=_scope_denied_message(scope),
            )
        return principal

    return _dependency


def _scope_denied_message(scope: ApiScope) -> str:
    if scope is ApiScope.AGENT_RUN:
        # The upgrade path is the useful half of this message — this is the
        # boundary between the $5/$10 plans and the $15 one, and it is the
        # refusal an integrator is most likely to hit.
        return (
            "This API key cannot run assistants. API consumption — driving an "
            "assistant as an agent from your own platform — is part of the "
            "Scale plan ($15/mo)."
        )
    return f"This API key is missing the {scope.value} scope."


RunAgentPrincipalDep = Annotated[Principal, Depends(require_scope(ApiScope.AGENT_RUN))]
AssistantsWritePrincipalDep = Annotated[
    Principal, Depends(require_scope(ApiScope.ASSISTANTS_WRITE))
]
KnowledgeWritePrincipalDep = Annotated[
    Principal, Depends(require_scope(ApiScope.KNOWLEDGE_WRITE))
]
AnalyticsReadPrincipalDep = Annotated[Principal, Depends(require_scope(ApiScope.ANALYTICS_READ))]
