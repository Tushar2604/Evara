"""Cross-tenant reads and controls for the Super Admin panel.

Deliberately thin: `PlatformAdminRepository` already does the aggregation, so
these use cases mostly just open a unit of work, call it, and raise
`NotFoundError` where a tenant/user id doesn't resolve. Kept as use cases
rather than calling the repository straight from the router so the router
stays uniform with the rest of the API (every other read here goes through a
use case) and so the two write paths (`set_tenant_status`, `set_user_status`)
have one place to add an audit-log write later if the product wants one.

Nothing in this module ever touches `chat_messages` or `rag_request_logs`
content (`query`/`answer`) — only counts, rates, and timestamps, per the
product's explicit privacy line for this panel.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.application.ports.repositories import (
    PlatformHealthDay,
    ProviderStat,
    TenantDetail,
    TenantOverview,
    UnitOfWork,
)
from src.domain.shared.errors import NotFoundError
from src.domain.shared.identifiers import TenantId, UserId


class ListTenantsOverview:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self) -> list[TenantOverview]:
        async with self._uow as uow:
            return await uow.platform_admin.tenants_overview()


class GetTenantDetail:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, tenant_id: TenantId) -> TenantDetail:
        async with self._uow as uow:
            detail = await uow.platform_admin.tenant_detail(tenant_id)
        if detail is None:
            raise NotFoundError("Tenant not found.")
        return detail


class GetPlatformHealth:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, days: int = 30) -> tuple[list[PlatformHealthDay], list[ProviderStat]]:
        since = datetime.now(UTC) - timedelta(days=days)
        async with self._uow as uow:
            daily = await uow.platform_admin.platform_health(since)
            providers = await uow.platform_admin.platform_provider_mix(since)
        return daily, providers


class SetTenantStatus:
    """Suspend or reactivate a whole workspace — the "control any
    mishappening" lever for a tenant acting badly (abuse, non-payment,
    a runaway integration)."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, tenant_id: TenantId, is_active: bool) -> None:
        async with self._uow as uow:
            if await uow.tenants.get(tenant_id) is None:
                raise NotFoundError("Tenant not found.")
            await uow.tenants.set_active(tenant_id, is_active)
            await uow.commit()


class SetUserStatus:
    """Suspend or reactivate one person's login, without touching their
    tenant or teammates."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, user_id: UserId, is_active: bool) -> None:
        async with self._uow as uow:
            if await uow.users.get(user_id) is None:
                raise NotFoundError("User not found.")
            await uow.users.set_active(user_id, is_active)
            await uow.commit()
