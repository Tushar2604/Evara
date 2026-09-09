"""Super Admin panel: cross-tenant visibility and the "suspend" controls.

Every route here is gated by `PlatformAdminPrincipalDep`, independent of the
tenant-scoped `require_admin` used by the rest of the "admin panel" surfaces —
a platform admin may hold any role, or none, in their own workspace.

Nothing returned here carries message content: no `chat_messages.content`, no
`rag_request_logs.query`/`.answer`. Only counts, rates, tiers, and timestamps —
see `application/use_cases/platform_admin.py` for the reasoning.
"""

from __future__ import annotations

from fastapi import APIRouter

from src.application.use_cases.platform_admin import (
    GetPlatformHealth,
    GetTenantDetail,
    ListTenantsOverview,
    SetTenantStatus,
    SetUserStatus,
)
from src.domain.shared.identifiers import TenantId, UserId
from src.interfaces.api.deps import ContainerDep, PlatformAdminPrincipalDep
from src.interfaces.api.schemas import (
    PlatformHealthDayResponse,
    PlatformHealthResponse,
    PlatformProviderStatResponse,
    PlatformUserResponse,
    SetStatusRequest,
    TenantDetailResponse,
    TenantOverviewResponse,
    UsageDayResponse,
)

router = APIRouter(prefix="/platform-admin", tags=["platform-admin"])


def _overview_schema(t) -> TenantOverviewResponse:  # noqa: ANN001
    return TenantOverviewResponse(
        tenant_id=t.tenant_id,
        name=t.name,
        slug=t.slug,
        is_active=t.is_active,
        created_at=t.created_at,
        plan_tier=t.plan_tier,
        subscription_status=t.subscription_status,
        user_count=t.user_count,
        assistant_count=t.assistant_count,
        tokens_today=t.tokens_today,
        tokens_30d=t.tokens_30d,
        requests_30d=t.requests_30d,
        error_rate_30d=t.error_rate_30d,
        refusal_rate_30d=t.refusal_rate_30d,
    )


@router.get("/tenants", response_model=list[TenantOverviewResponse])
async def list_tenants(
    _principal: PlatformAdminPrincipalDep, container: ContainerDep
) -> list[TenantOverviewResponse]:
    rows = await ListTenantsOverview(container.unit_of_work()).execute()
    return [_overview_schema(t) for t in rows]


@router.get("/tenants/{tenant_id}", response_model=TenantDetailResponse)
async def get_tenant(
    tenant_id: TenantId, _principal: PlatformAdminPrincipalDep, container: ContainerDep
) -> TenantDetailResponse:
    detail = await GetTenantDetail(container.unit_of_work()).execute(tenant_id)
    return TenantDetailResponse(
        overview=_overview_schema(detail.overview),
        users=[
            PlatformUserResponse(
                user_id=u.user_id,
                email=u.email,
                role=u.role,
                is_active=u.is_active,
                last_login_at=u.last_login_at,
                created_at=u.created_at,
            )
            for u in detail.users
        ],
        usage_daily=[
            UsageDayResponse(day=d.day, tokens_used=d.tokens_used) for d in detail.usage_daily
        ],
    )


@router.post("/tenants/{tenant_id}/status", response_model=TenantOverviewResponse)
async def set_tenant_status(
    tenant_id: TenantId,
    body: SetStatusRequest,
    _principal: PlatformAdminPrincipalDep,
    container: ContainerDep,
) -> TenantOverviewResponse:
    uow = container.unit_of_work()
    await SetTenantStatus(uow).execute(tenant_id, body.is_active)
    detail = await GetTenantDetail(container.unit_of_work()).execute(tenant_id)
    return _overview_schema(detail.overview)


@router.post("/users/{user_id}/status")
async def set_user_status(
    user_id: UserId,
    body: SetStatusRequest,
    _principal: PlatformAdminPrincipalDep,
    container: ContainerDep,
) -> dict[str, bool]:
    await SetUserStatus(container.unit_of_work()).execute(user_id, body.is_active)
    return {"is_active": body.is_active}


@router.get("/health", response_model=PlatformHealthResponse)
async def platform_health(
    _principal: PlatformAdminPrincipalDep, container: ContainerDep, days: int = 30
) -> PlatformHealthResponse:
    daily, providers = await GetPlatformHealth(container.unit_of_work()).execute(
        days=min(max(days, 1), 90)
    )
    return PlatformHealthResponse(
        days=days,
        daily=[
            PlatformHealthDayResponse(
                day=d.day,
                answers=d.answers,
                error_rate=d.error_rate,
                refusal_rate=d.refusal_rate,
                avg_latency_ms=d.avg_latency_ms,
            )
            for d in daily
        ],
        providers=[
            PlatformProviderStatResponse(
                provider=p.provider, answers=p.answers, avg_top_score=p.avg_top_score,
                avg_tokens=p.avg_tokens,
            )
            for p in providers
        ],
    )
