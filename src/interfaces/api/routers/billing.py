"""Billing: what the workspace is on, what it grants, and how to change it.

Reads are open to any signed-in member — a developer needs to know which tier
they are building against, and hiding the plan from them only produces support
tickets. Writes are Owner/Admin (`AdminPrincipalDep`), because changing the
plan is changing what the account is billed.

`POST /billing/subscription` records a plan change; it does not take payment.
The seam where a payment processor belongs is documented in
`application/use_cases/billing.py` — read that before exposing this to
self-service checkout.
"""

from __future__ import annotations

from fastapi import APIRouter

from src.application.use_cases.billing import (
    CancelSubscription,
    ChangePlan,
    Entitlements,
    GetBillingOverview,
    ListBillingHistory,
)
from src.domain.billing.entities import PLANS, Plan, PlanTier
from src.interfaces.api.deps import AdminPrincipalDep, ContainerDep, PrincipalDep
from src.interfaces.api.schemas import (
    BillingOverviewResponse,
    BillingTransactionResponse,
    ChangePlanRequest,
    PlanSchema,
    PlanUsageSchema,
    SubscriptionSchema,
)

router = APIRouter(prefix="/billing", tags=["billing"])


def _plan_schema(plan: Plan) -> PlanSchema:
    return PlanSchema(
        tier=plan.tier.value,
        name=plan.name,
        price_usd=plan.price_usd,
        tagline=plan.tagline,
        max_assistants=plan.max_assistants,
        max_documents=plan.max_documents,
        daily_token_quota=plan.daily_token_quota,
        monthly_api_calls=plan.monthly_api_calls,
        scopes=sorted(s.value for s in plan.scopes),
        api_access=plan.api_access,
        agent_api=plan.agent_api,
    )


def _overview(entitlements: Entitlements) -> BillingOverviewResponse:
    sub = entitlements.subscription
    return BillingOverviewResponse(
        subscription=SubscriptionSchema(
            # The *effective* tier, so a workspace whose paid period has lapsed
            # is shown as Free rather than as a plan it is no longer getting.
            tier=sub.effective_tier.value,
            status=sub.status.value,
            started_at=sub.started_at,
            current_period_end=sub.current_period_end,
            auto_renew=sub.auto_renew,
            canceled_at=sub.canceled_at,
        ),
        plan=_plan_schema(entitlements.plan),
        usage=PlanUsageSchema(
            assistants_used=entitlements.assistants_used,
            assistants_remaining=entitlements.assistants_remaining,
            documents_used=entitlements.documents_used,
            tokens_used_today=entitlements.tokens_used_today,
            api_calls_this_period=entitlements.api_calls_this_period,
            api_calls_remaining=entitlements.api_calls_remaining,
        ),
        # Ordered cheapest first, which is the order the pricing table reads in.
        available_plans=[_plan_schema(PLANS[t]) for t in PlanTier],
    )


@router.get("", response_model=BillingOverviewResponse)
async def get_billing(
    principal: PrincipalDep, container: ContainerDep
) -> BillingOverviewResponse:
    use_case = GetBillingOverview(container.unit_of_work())
    return _overview(await use_case.execute(principal.tenant_id))


@router.get("/plans", response_model=list[PlanSchema])
async def list_plans() -> list[PlanSchema]:
    """The catalogue on its own — unauthenticated, so a pricing page can read
    it without a session. It contains no tenant data."""
    return [_plan_schema(PLANS[tier]) for tier in PlanTier]


@router.post("/subscription", response_model=BillingOverviewResponse)
async def change_plan(
    body: ChangePlanRequest, principal: AdminPrincipalDep, container: ContainerDep
) -> BillingOverviewResponse:
    use_case = ChangePlan(container.unit_of_work())
    entitlements = await use_case.execute(
        principal.tenant_id, PlanTier(body.tier), reference=body.reference
    )
    return _overview(entitlements)


@router.delete("/subscription", response_model=BillingOverviewResponse)
async def cancel_plan(
    principal: AdminPrincipalDep, container: ContainerDep
) -> BillingOverviewResponse:
    use_case = CancelSubscription(container.unit_of_work())
    return _overview(await use_case.execute(principal.tenant_id))


@router.get("/transactions", response_model=list[BillingTransactionResponse])
async def list_transactions(
    principal: AdminPrincipalDep, container: ContainerDep, limit: int = 50
) -> list[BillingTransactionResponse]:
    use_case = ListBillingHistory(container.unit_of_work())
    rows = await use_case.execute(principal.tenant_id, limit=min(limit, 200))
    return [
        BillingTransactionResponse(
            id=t.id,
            kind=t.kind,
            amount_usd=t.amount_usd,
            description=t.description,
            plan_tier=t.plan_tier,
            reference=t.reference,
            created_at=t.created_at,
        )
        for t in rows
    ]
