"""Billing use cases: read the plan, change it, and read the history.

The one function worth knowing about here is `resolve_entitlements` — every
enforcement point in the system goes through it, so there is exactly one answer
to "what is this workspace allowed to do" and it is computed from the
subscription row rather than assembled ad hoc at each call site.

What this module does *not* do is take money. `ChangePlan` records the change
and writes a billing line; it does not talk to a payment processor, because
there isn't one wired up yet. That seam is deliberate and narrow: when Stripe
(or whoever) arrives, it authorises *before* `ChangePlan.execute` is called and
passes its charge id in as `reference`. Nothing else in the codebase has to
know. Until then a deployment that exposes plan changes to end users directly
is giving plans away, which is why the router puts it behind Owner/Admin.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.application.ports.repositories import UnitOfWork
from src.domain.billing.entities import (
    BillingTransaction,
    Plan,
    PlanTier,
    Subscription,
)
from src.domain.shared.errors import InvalidStateError, QuotaExceededError
from src.domain.shared.identifiers import TenantId


@dataclass(frozen=True)
class Entitlements:
    """The plan, plus what the workspace has actually used against it.

    Bundled because every consumer needs both halves: a limit without the
    current count cannot answer "may I create one more", and a count without the
    limit cannot be rendered.
    """

    subscription: Subscription
    plan: Plan
    assistants_used: int
    documents_used: int
    tokens_used_today: int
    api_calls_this_period: int

    @property
    def tier(self) -> PlanTier:
        return self.plan.tier

    @property
    def assistants_remaining(self) -> int | None:
        """None = unlimited, which is what every paid tier gets."""
        if self.plan.max_assistants is None:
            return None
        return max(0, self.plan.max_assistants - self.assistants_used)

    @property
    def api_calls_remaining(self) -> int:
        return max(0, self.plan.monthly_api_calls - self.api_calls_this_period)


def period_start(subscription: Subscription) -> datetime:
    """The start of the billing window the API allowance is counted over.

    Derived from `current_period_end` so the meter and the invoice agree. A free
    (or never-subscribed) workspace has no period end, so it falls back to a
    rolling 30 days — it has no allowance to spend anyway, and a rolling window
    is the honest reading of "calls recently".
    """
    if subscription.current_period_end is not None:
        return subscription.current_period_end - timedelta(days=30)
    return datetime.now(UTC) - timedelta(days=30)


async def load_subscription(uow: UnitOfWork, tenant_id: TenantId) -> Subscription:
    """The tenant's subscription, or the free default for one that has none."""
    return await uow.subscriptions.get(tenant_id) or Subscription.free(tenant_id)


async def reconcile_tenant_limits(uow: UnitOfWork, tenant_id: TenantId) -> None:
    """Bring the tenant's stored ceilings back in line with its live plan.

    `ChangePlan` writes them at the moment of purchase, which covers upgrades
    and downgrades. It cannot cover *lapsing* — nobody calls an endpoint when a
    period simply ends — so a canceled Growth workspace would otherwise keep
    Growth's token quota indefinitely.

    Rather than a cron sweep, the limits converge whenever entitlements are
    consulted: opening Billing, creating an assistant, authenticating an API
    key. The write is skipped when nothing differs, so the common case costs one
    comparison. The bounded lag this leaves is on the daily token quota only —
    assistant ceilings and API scopes are computed from the live plan on every
    check and are never stale.
    """
    subscription = await load_subscription(uow, tenant_id)
    plan = subscription.plan
    tenant = await uow.tenants.get(tenant_id)
    if tenant is None:
        return
    if (
        tenant.daily_token_quota == plan.daily_token_quota
        and tenant.max_documents == plan.max_documents
    ):
        return
    await uow.tenants.set_limits(
        tenant_id,
        daily_token_quota=plan.daily_token_quota,
        max_documents=plan.max_documents,
    )


async def resolve_entitlements(uow: UnitOfWork, tenant_id: TenantId) -> Entitlements:
    """What this workspace may do, and what it has used. The single source of
    truth for every limit check in the system.

    Note it reads the *effective* plan (`Subscription.plan`), so a lapsed or
    expired paid subscription resolves to Free here rather than at each call
    site — there is no path by which an unpaid workspace keeps its ceiling.
    """
    await reconcile_tenant_limits(uow, tenant_id)
    subscription = await load_subscription(uow, tenant_id)
    plan = subscription.plan

    assistants = len(await uow.chatbots.list_for_tenant(tenant_id))
    documents = await uow.documents.count_for_tenant(tenant_id)
    tokens = await uow.usage.tokens_used_today(tenant_id)
    api_calls = await uow.api_usage.calls_since(tenant_id, period_start(subscription).date())

    return Entitlements(
        subscription=subscription,
        plan=plan,
        assistants_used=assistants,
        documents_used=documents,
        tokens_used_today=tokens,
        api_calls_this_period=api_calls,
    )


async def ensure_can_create_assistant(uow: UnitOfWork, tenant_id: TenantId) -> None:
    """Raise unless the plan has room for one more assistant.

    Called inside the caller's own transaction, immediately before the insert,
    so the count it checks is the count that will be true. Every creation path
    goes through here — the plain create and both generate paths — because a
    ceiling enforced on two of three routes is not a ceiling.

    `QuotaExceededError` maps to 429 (`interfaces/api/errors.py`), which is the
    established shape for "you hit a limit" in this codebase.
    """
    await reconcile_tenant_limits(uow, tenant_id)
    subscription = await load_subscription(uow, tenant_id)
    plan = subscription.plan
    if plan.max_assistants is None:
        return

    count = len(await uow.chatbots.list_for_tenant(tenant_id))
    if not plan.allows_assistant(count):
        raise QuotaExceededError(
            f"The {plan.name} plan includes {plan.max_assistants} assistant"
            f"{'' if plan.max_assistants == 1 else 's'}. Upgrade to Starter "
            f"($5/mo) for unlimited assistants."
        )


class GetBillingOverview:
    """Everything the Billing page renders in one round trip."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, tenant_id: TenantId) -> Entitlements:
        async with self._uow as uow:
            uow.set_tenant_scope(tenant_id)
            entitlements = await resolve_entitlements(uow, tenant_id)
            # Committed because reading entitlements can reconcile a lapsed
            # plan's stored ceilings (see `reconcile_tenant_limits`). Without
            # this the correction is computed and then rolled back on every
            # page load, which is the same as never making it.
            await uow.commit()
            return entitlements


class ChangePlan:
    """Move a workspace onto a tier and write the billing line for it."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        tenant_id: TenantId,
        tier: PlanTier,
        *,
        reference: str | None = None,
    ) -> Entitlements:
        async with self._uow as uow:
            uow.set_tenant_scope(tenant_id)
            subscription = await load_subscription(uow, tenant_id)

            if subscription.tier is tier and subscription.is_current:
                raise InvalidStateError(f"This workspace is already on the {tier.value} plan.")

            plan = _plan_or_fail(tier)
            subscription.change_to(tier)
            await uow.subscriptions.upsert(subscription)

            # Push the plan's ceilings onto the tenant row, where the ingestion
            # and answering paths already read them. This is what stops the
            # pricing table from being decorative: a workspace that buys Growth
            # gets Growth's token quota in the same transaction, and there is no
            # second copy of "what is this tenant's limit" to fall out of sync.
            await uow.tenants.set_limits(
                tenant_id,
                daily_token_quota=plan.daily_token_quota,
                max_documents=plan.max_documents,
            )

            # A move to Free is a downgrade, not a $0 purchase — recording it as
            # a transaction keeps the history a record of what happened rather
            # than only of what was paid.
            await uow.billing_transactions.add(
                BillingTransaction(
                    tenant_id=tenant_id,
                    kind="subscription" if tier is not PlanTier.FREE else "adjustment",
                    amount_usd=plan.price_usd,
                    description=(
                        f"Subscribed to {plan.name} (${plan.price_usd:.2f}/mo)"
                        if tier is not PlanTier.FREE
                        else "Moved to the Free plan"
                    ),
                    plan_tier=tier.value,
                    reference=reference,
                )
            )
            await uow.commit()

            # Recomputed after the commit-in-progress state so the caller gets
            # the entitlements that now apply, not the ones that just expired.
            return await resolve_entitlements(uow, tenant_id)


class CancelSubscription:
    """Stop the renewal. Entitlements survive to the end of the paid period —
    see `Subscription.cancel`."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, tenant_id: TenantId) -> Entitlements:
        async with self._uow as uow:
            uow.set_tenant_scope(tenant_id)
            subscription = await load_subscription(uow, tenant_id)
            if subscription.tier is PlanTier.FREE:
                raise InvalidStateError("There is no paid plan to cancel.")

            subscription.cancel()
            await uow.subscriptions.upsert(subscription)
            await uow.billing_transactions.add(
                BillingTransaction(
                    tenant_id=tenant_id,
                    kind="adjustment",
                    amount_usd=0.0,
                    description="Canceled — access continues until the period ends",
                    plan_tier=subscription.tier.value,
                )
            )
            await uow.commit()
            return await resolve_entitlements(uow, tenant_id)


class ListBillingHistory:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, tenant_id: TenantId, limit: int = 50) -> list[BillingTransaction]:
        async with self._uow as uow:
            uow.set_tenant_scope(tenant_id)
            return await uow.billing_transactions.list_for_tenant(tenant_id, limit=limit)


def _plan_or_fail(tier: PlanTier) -> Plan:
    from src.domain.billing.entities import PLANS

    plan = PLANS.get(tier)
    if plan is None:  # pragma: no cover - PlanTier is closed, this is a guard
        raise InvalidStateError(f"Unknown plan: {tier}")
    return plan
