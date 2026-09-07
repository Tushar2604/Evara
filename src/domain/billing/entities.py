"""Plans, subscriptions, and the entitlements that follow from them.

One idea runs through this module: *the plan is the only source of truth for
what a workspace can do*. Nothing else — not a role, not a feature flag, not a
column somebody set by hand — decides whether a tenant may create a thirtieth
assistant or call the agent over HTTP. Two consequences worth stating up front,
because the rest of the codebase depends on both:

  * An API key can never out-rank the plan that issued it. Scopes are stamped
    onto the key at creation time from `Plan.scopes`, and re-checked against the
    *live* plan on every request. A workspace that downgrades from Scale to
    Starter does not get to keep calling `agent.run` with the key it minted last
    month, and we need no revocation sweep to make that true.
  * The free tier issues no keys at all. Not "keys with no scopes" — none. The
    programmatic surface is the paid product, so the gate belongs at creation.

Prices are the published monthly figures in USD, held here rather than in
settings because the *shape* of a plan (what it unlocks) and its price move
together; splitting them across two files is how a $15 plan ends up without
agent access on one deploy.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from src.domain.shared.identifiers import TenantId, new_id


class PlanTier(StrEnum):
    """The four categories an account can be in.

    Ordered from least to most capable; `rank()` makes that order comparable so
    call sites can ask "at least Starter?" instead of enumerating tiers.
    """

    FREE = "free"
    STARTER = "starter"  # $5/mo
    GROWTH = "growth"  # $10/mo
    SCALE = "scale"  # $15/mo — the API-consumption tier

    def rank(self) -> int:
        return _TIER_ORDER.index(self)

    def at_least(self, other: PlanTier) -> bool:
        return self.rank() >= other.rank()


_TIER_ORDER: tuple[PlanTier, ...] = (
    PlanTier.FREE,
    PlanTier.STARTER,
    PlanTier.GROWTH,
    PlanTier.SCALE,
)


class ApiScope(StrEnum):
    """What a key is allowed to do.

    Deliberately coarse — one scope per *surface*, not per endpoint. Fine-grained
    scopes look rigorous and end up either unused (everyone ticks every box) or
    wrong (a new endpoint lands in no scope and is silently ungated). Five is
    enough to express the only distinction customers actually pay for: managing
    assistants versus *running* them.
    """

    ASSISTANTS_READ = "assistants.read"
    ASSISTANTS_WRITE = "assistants.write"
    KNOWLEDGE_WRITE = "knowledge.write"
    ANALYTICS_READ = "analytics.read"
    # The paid-for one. Everything that spends model tokens on the caller's
    # behalf — chat turns, agent runs, streaming — sits behind this and this
    # alone, so "can this key make the assistant *think*" is a single check.
    AGENT_RUN = "agent.run"


# The management surface: create and configure assistants, feed them knowledge,
# read what happened. Shared by every paid tier.
_MANAGEMENT_SCOPES: frozenset[ApiScope] = frozenset(
    {
        ApiScope.ASSISTANTS_READ,
        ApiScope.ASSISTANTS_WRITE,
        ApiScope.KNOWLEDGE_WRITE,
        ApiScope.ANALYTICS_READ,
    }
)


class SubscriptionStatus(StrEnum):
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Plan:
    """A tier's published price and its entitlements.

    `max_assistants=None` means unlimited — the headline of the $5 and $10
    plans. `None` rather than a large sentinel, so "unlimited" is a state the
    type shows you rather than a number you have to recognise.
    """

    tier: PlanTier
    name: str
    price_usd: float
    tagline: str
    max_assistants: int | None
    max_documents: int
    daily_token_quota: int
    monthly_api_calls: int
    scopes: frozenset[ApiScope]

    @property
    def api_access(self) -> bool:
        """Can this plan mint API keys at all? The free tier cannot."""
        return bool(self.scopes)

    @property
    def agent_api(self) -> bool:
        """Does this plan include API consumption — an assistant driven as an
        agent from someone else's platform?"""
        return ApiScope.AGENT_RUN in self.scopes

    def allows_assistant(self, current_count: int) -> bool:
        return self.max_assistants is None or current_count < self.max_assistants


PLANS: dict[PlanTier, Plan] = {
    PlanTier.FREE: Plan(
        tier=PlanTier.FREE,
        name="Free",
        price_usd=0.0,
        tagline="Try it out. One assistant, no programmatic access.",
        # One, not zero: a workspace that cannot build the thing has nothing to
        # evaluate and no reason to upgrade.
        max_assistants=1,
        max_documents=20,
        daily_token_quota=200_000,
        monthly_api_calls=0,
        scopes=frozenset(),
    ),
    PlanTier.STARTER: Plan(
        tier=PlanTier.STARTER,
        name="Starter",
        price_usd=5.0,
        tagline="Unlimited assistants and the management API.",
        max_assistants=None,
        max_documents=200,
        daily_token_quota=2_000_000,
        monthly_api_calls=10_000,
        scopes=_MANAGEMENT_SCOPES,
    ),
    PlanTier.GROWTH: Plan(
        tier=PlanTier.GROWTH,
        name="Growth",
        price_usd=10.0,
        tagline="Unlimited assistants, higher limits, the management API.",
        max_assistants=None,
        max_documents=1_000,
        daily_token_quota=5_000_000,
        monthly_api_calls=50_000,
        scopes=_MANAGEMENT_SCOPES,
    ),
    PlanTier.SCALE: Plan(
        tier=PlanTier.SCALE,
        name="Scale",
        price_usd=15.0,
        tagline=(
            "Everything in Growth, plus API consumption: drive an assistant as "
            "an agent from your own platform."
        ),
        max_assistants=None,
        max_documents=5_000,
        daily_token_quota=20_000_000,
        monthly_api_calls=250_000,
        scopes=_MANAGEMENT_SCOPES | {ApiScope.AGENT_RUN},
    ),
}


def plan_for(tier: PlanTier | str) -> Plan:
    """The plan for a tier, tolerating an unknown string as Free.

    Unknown falls back rather than raising because this runs on the request path
    against a value read from a row: a tier this build does not recognise (an
    older deploy, a hand-edited row) must degrade to the least-privileged plan,
    never 500 into an open door.
    """
    try:
        return PLANS[PlanTier(tier)]
    except ValueError:
        return PLANS[PlanTier.FREE]


@dataclass
class Subscription:
    """What a tenant is currently paying for.

    Exactly one row per tenant. A tenant with *no* row is Free — `free()` builds
    that default rather than the repository inventing a row, so signing up
    writes no billing record and the free tier stays the absence of a decision.
    """

    tenant_id: TenantId
    tier: PlanTier = PlanTier.FREE
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE
    id: uuid.UUID = field(default_factory=new_id)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # None = it never lapses, which is exactly what the free tier is.
    current_period_end: datetime | None = None
    auto_renew: bool = True
    canceled_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def free(cls, tenant_id: TenantId) -> Subscription:
        return cls(tenant_id=tenant_id, tier=PlanTier.FREE)

    @property
    def is_current(self) -> bool:
        """Is the workspace entitled to its tier *right now*?

        A canceled subscription keeps its entitlements until the period already
        paid for runs out — cancelling on day 3 of a month you bought should not
        take the product away on day 3.
        """
        if self.status is SubscriptionStatus.EXPIRED:
            return False
        return not (
            self.current_period_end is not None
            and self.current_period_end <= datetime.now(UTC)
        )

    @property
    def effective_tier(self) -> PlanTier:
        """The tier to actually enforce. A lapsed paid plan is Free."""
        return self.tier if self.is_current else PlanTier.FREE

    @property
    def plan(self) -> Plan:
        return plan_for(self.effective_tier)

    def change_to(self, tier: PlanTier, *, period_days: int = 30) -> None:
        now = datetime.now(UTC)
        self.tier = tier
        self.status = SubscriptionStatus.ACTIVE
        self.canceled_at = None
        self.started_at = now
        self.current_period_end = (
            None if tier is PlanTier.FREE else now + timedelta(days=period_days)
        )
        self.auto_renew = tier is not PlanTier.FREE
        self.updated_at = now

    def cancel(self) -> None:
        """Stop the renewal; keep the entitlements until the period ends."""
        now = datetime.now(UTC)
        self.status = SubscriptionStatus.CANCELED
        self.auto_renew = False
        self.canceled_at = now
        self.updated_at = now


@dataclass
class BillingTransaction:
    """One line of billing history — a plan charge, a wallet top-up, a credit.

    Append-only: nothing here is ever updated, so the history a customer reads is
    the history that happened.
    """

    tenant_id: TenantId
    kind: str  # "subscription" | "topup" | "credit" | "adjustment"
    amount_usd: float
    description: str
    id: uuid.UUID = field(default_factory=new_id)
    plan_tier: str | None = None
    reference: str | None = None  # the payment-provider id, once there is one
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
