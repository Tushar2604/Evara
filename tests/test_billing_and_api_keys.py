"""The two rules the paid API rests on, tested against fakes.

  1. Only a paid plan can mint a key, and a key never carries a scope its plan
     does not grant.
  2. A key's power is re-derived from the *live* plan on every request, so a
     downgrade takes effect immediately and without a revocation sweep.

Everything else in the billing feature is bookkeeping around those two, so the
tests here go after them directly rather than through HTTP — the interesting
decisions all live in the use-case and domain layers.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from src.application.use_cases.api_keys import (
    CreateApiKey,
    RevokeApiKey,
    effective_scopes,
    generate_secret_key,
)
from src.application.use_cases.billing import (
    ChangePlan,
    ensure_can_create_assistant,
    resolve_entitlements,
)
from src.config.container import get_container
from src.domain.billing.entities import (
    PLANS,
    ApiScope,
    PlanTier,
    Subscription,
    SubscriptionStatus,
    plan_for,
)
from src.domain.chatbot.entities import Chatbot
from src.domain.shared.errors import PermissionDeniedError, QuotaExceededError
from src.domain.shared.identifiers import TenantId
from src.domain.tenant.entities import ApiKey, Tenant
from src.infrastructure.security.hashing import hash_api_key
from src.interfaces.api.app import create_app
from src.interfaces.api.deps import container_dep

TENANT_ID = TenantId(uuid.uuid4())


# --- Fakes -------------------------------------------------------------------


class FakeSubscriptions:
    def __init__(self, subscription: Subscription | None = None) -> None:
        self.subscription = subscription

    async def get(self, tenant_id: TenantId) -> Subscription | None:
        return self.subscription

    async def upsert(self, subscription: Subscription) -> None:
        self.subscription = subscription


class FakeApiKeys:
    def __init__(self) -> None:
        self.items: list[ApiKey] = []

    async def add(self, key: ApiKey) -> None:
        self.items.append(key)

    async def get_by_hash(self, key_hash: str) -> ApiKey | None:
        return next((k for k in self.items if k.key_hash == key_hash and k.is_active), None)

    async def get(self, tenant_id: TenantId, key_id: uuid.UUID) -> ApiKey | None:
        return next((k for k in self.items if k.id == key_id), None)

    async def list_for_tenant(self, tenant_id: TenantId) -> list[ApiKey]:
        return list(self.items)

    async def revoke(self, tenant_id: TenantId, key_id: uuid.UUID) -> bool:
        for k in self.items:
            if k.id == key_id and k.is_active:
                k.revoke()
                return True
        return False

    async def touch(self, key_id: uuid.UUID) -> None:
        return None


class FakeChatbots:
    def __init__(self, count: int = 0) -> None:
        self.items = [Chatbot(tenant_id=TENANT_ID, name=f"Bot {i}") for i in range(count)]

    async def list_for_tenant(self, tenant_id: TenantId) -> list[Chatbot]:
        return self.items


class FakeTenants:
    def __init__(self) -> None:
        self.tenant = Tenant(name="Acme", slug="acme", id=TENANT_ID)
        self.set_limits_calls: list[tuple[int, int]] = []

    async def get(self, tenant_id: TenantId) -> Tenant | None:
        return self.tenant

    async def set_limits(
        self, tenant_id: TenantId, *, daily_token_quota: int, max_documents: int
    ) -> None:
        self.tenant.daily_token_quota = daily_token_quota
        self.tenant.max_documents = max_documents
        self.set_limits_calls.append((daily_token_quota, max_documents))


class FakeDocuments:
    async def count_for_tenant(self, tenant_id: TenantId) -> int:
        return 0


class FakeUsage:
    async def tokens_used_today(self, tenant_id: TenantId) -> int:
        return 0


class FakeApiUsage:
    def __init__(self) -> None:
        self.calls = 0

    async def record_call(self, tenant_id: TenantId) -> None:
        self.calls += 1

    async def calls_since(self, tenant_id: TenantId, since) -> int:  # noqa: ANN001
        return self.calls


class FakeBillingTransactions:
    def __init__(self) -> None:
        self.items: list = []

    async def add(self, transaction) -> None:  # noqa: ANN001
        self.items.append(transaction)

    async def list_for_tenant(self, tenant_id: TenantId, limit: int = 50) -> list:
        return self.items


class FakeUow:
    def __init__(
        self,
        *,
        subscription: Subscription | None = None,
        assistants: int = 0,
    ) -> None:
        self.subscriptions = FakeSubscriptions(subscription)
        self.api_keys = FakeApiKeys()
        self.chatbots = FakeChatbots(assistants)
        self.tenants = FakeTenants()
        self.documents = FakeDocuments()
        self.usage = FakeUsage()
        self.api_usage = FakeApiUsage()
        self.billing_transactions = FakeBillingTransactions()
        self.committed = 0

    def set_tenant_scope(self, tenant_id: TenantId) -> None:
        self.scope = tenant_id

    async def commit(self) -> None:
        self.committed += 1

    async def __aenter__(self) -> FakeUow:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def uow_factory(uow: FakeUow):  # noqa: ANN201
    """`container.unit_of_work()` hands a use case a fresh context manager; here
    every call yields the same in-memory one so assertions can read it after."""

    @asynccontextmanager
    async def _factory():  # noqa: ANN202
        yield uow

    return _factory()


def paid(tier: PlanTier) -> Subscription:
    sub = Subscription(tenant_id=TENANT_ID)
    sub.change_to(tier)
    return sub


# --- Who may mint a key ------------------------------------------------------


class TestKeyIssuance:
    @pytest.mark.asyncio
    async def test_the_free_tier_is_refused_and_told_where_to_go(self) -> None:
        uow = FakeUow()  # no subscription row at all — the free default
        with pytest.raises(PermissionDeniedError) as exc:
            await CreateApiKey(uow_factory(uow)).execute(TENANT_ID, name="ci")

        # The refusal has to carry the upgrade path, or it is a dead end.
        assert "Starter" in str(exc.value)
        assert uow.api_keys.items == []

    @pytest.mark.asyncio
    async def test_a_starter_key_gets_management_scopes_but_not_agent_run(self) -> None:
        uow = FakeUow(subscription=paid(PlanTier.STARTER))
        issued = await CreateApiKey(uow_factory(uow)).execute(TENANT_ID, name="ci")

        assert ApiScope.ASSISTANTS_WRITE.value in issued.key.scopes
        assert ApiScope.AGENT_RUN.value not in issued.key.scopes
        assert issued.key.plan_tier == PlanTier.STARTER.value

    @pytest.mark.asyncio
    async def test_only_the_scale_plan_grants_agent_run(self) -> None:
        uow = FakeUow(subscription=paid(PlanTier.SCALE))
        issued = await CreateApiKey(uow_factory(uow)).execute(TENANT_ID, name="agent")

        assert ApiScope.AGENT_RUN.value in issued.key.scopes

    @pytest.mark.asyncio
    async def test_asking_for_agent_run_below_scale_is_refused_not_narrowed(self) -> None:
        """The quiet-downgrade failure mode: a key that came back with fewer
        permissions than asked for would fail later, in the caller's
        production, on an endpoint they believed they had bought."""
        uow = FakeUow(subscription=paid(PlanTier.GROWTH))
        with pytest.raises(PermissionDeniedError) as exc:
            await CreateApiKey(uow_factory(uow)).execute(
                TENANT_ID, name="agent", requested_scopes=[ApiScope.AGENT_RUN.value]
            )

        assert "Scale" in str(exc.value)
        assert uow.api_keys.items == []

    @pytest.mark.asyncio
    async def test_the_raw_key_is_returned_once_and_stored_only_as_a_hash(self) -> None:
        uow = FakeUow(subscription=paid(PlanTier.SCALE))
        issued = await CreateApiKey(uow_factory(uow)).execute(TENANT_ID, name="ci")

        stored = uow.api_keys.items[0]
        assert issued.raw_key.startswith("sk_")
        assert stored.key_hash == hash_api_key(issued.raw_key)
        assert issued.raw_key not in (stored.key_hash, stored.prefix)
        # The prefix is short enough to be useless on its own.
        assert stored.prefix == issued.raw_key[:12]

    @pytest.mark.asyncio
    async def test_a_revoked_key_stops_authenticating_immediately(self) -> None:
        uow = FakeUow(subscription=paid(PlanTier.SCALE))
        issued = await CreateApiKey(uow_factory(uow)).execute(TENANT_ID, name="ci")

        await RevokeApiKey(uow_factory(uow)).execute(TENANT_ID, issued.key.id)

        assert await uow.api_keys.get_by_hash(hash_api_key(issued.raw_key)) is None


# --- What a key may do today -------------------------------------------------


class TestEffectiveScopes:
    def test_a_downgrade_strips_agent_run_from_a_key_minted_under_scale(self) -> None:
        key = ApiKey(
            tenant_id=TENANT_ID,
            name="agent",
            key_hash="x",
            prefix="sk_x",
            scopes=sorted(s.value for s in PLANS[PlanTier.SCALE].scopes),
            plan_tier=PlanTier.SCALE.value,
        )

        # The key's own row still claims agent.run...
        assert ApiScope.AGENT_RUN.value in key.scopes
        # ...and the live plan is what settles it.
        assert ApiScope.AGENT_RUN not in effective_scopes(key, PlanTier.STARTER)
        assert ApiScope.ASSISTANTS_WRITE in effective_scopes(key, PlanTier.STARTER)

    def test_dropping_to_free_leaves_a_key_with_nothing(self) -> None:
        key = ApiKey(
            tenant_id=TENANT_ID,
            name="agent",
            key_hash="x",
            prefix="sk_x",
            scopes=sorted(s.value for s in PLANS[PlanTier.SCALE].scopes),
            plan_tier=PlanTier.SCALE.value,
        )
        assert effective_scopes(key, PlanTier.FREE) == frozenset()

    def test_a_scope_this_build_no_longer_knows_grants_nothing(self) -> None:
        key = ApiKey(
            tenant_id=TENANT_ID,
            name="old",
            key_hash="x",
            prefix="sk_x",
            scopes=["billing.superuser"],
            plan_tier=PlanTier.SCALE.value,
        )
        assert effective_scopes(key, PlanTier.SCALE) == frozenset()


# --- Lapsing -----------------------------------------------------------------


class TestLapsedSubscriptions:
    def test_a_period_that_has_ended_resolves_to_free(self) -> None:
        sub = Subscription(
            tenant_id=TENANT_ID,
            tier=PlanTier.SCALE,
            current_period_end=datetime.now(UTC) - timedelta(days=1),
        )
        assert sub.effective_tier is PlanTier.FREE
        assert sub.plan.agent_api is False

    def test_cancelling_keeps_access_until_the_paid_period_ends(self) -> None:
        sub = paid(PlanTier.SCALE)
        sub.cancel()

        assert sub.status is SubscriptionStatus.CANCELED
        assert sub.auto_renew is False
        # Still Scale — they paid for this month.
        assert sub.effective_tier is PlanTier.SCALE

    @pytest.mark.asyncio
    async def test_reading_entitlements_pulls_a_lapsed_tenants_quota_back_down(self) -> None:
        lapsed = Subscription(
            tenant_id=TENANT_ID,
            tier=PlanTier.SCALE,
            current_period_end=datetime.now(UTC) - timedelta(days=1),
        )
        uow = FakeUow(subscription=lapsed)
        uow.tenants.tenant.daily_token_quota = PLANS[PlanTier.SCALE].daily_token_quota

        entitlements = await resolve_entitlements(uow, TENANT_ID)

        assert entitlements.plan.tier is PlanTier.FREE
        assert uow.tenants.tenant.daily_token_quota == PLANS[PlanTier.FREE].daily_token_quota


# --- The assistant ceiling ---------------------------------------------------


class TestAssistantCeiling:
    @pytest.mark.asyncio
    async def test_the_free_tier_stops_at_one_assistant(self) -> None:
        uow = FakeUow(assistants=1)
        with pytest.raises(QuotaExceededError) as exc:
            await ensure_can_create_assistant(uow, TENANT_ID)

        assert "Starter" in str(exc.value)

    @pytest.mark.asyncio
    async def test_a_free_workspace_with_none_yet_may_create_one(self) -> None:
        uow = FakeUow(assistants=0)
        await ensure_can_create_assistant(uow, TENANT_ID)  # does not raise

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tier", [PlanTier.STARTER, PlanTier.GROWTH, PlanTier.SCALE])
    async def test_every_paid_tier_is_unlimited(self, tier: PlanTier) -> None:
        uow = FakeUow(subscription=paid(tier), assistants=250)
        await ensure_can_create_assistant(uow, TENANT_ID)  # does not raise

        assert plan_for(tier).max_assistants is None


# --- Changing plan -----------------------------------------------------------


class TestChangePlan:
    @pytest.mark.asyncio
    async def test_subscribing_writes_the_plans_ceilings_onto_the_tenant(self) -> None:
        """The check that stops the pricing table being decorative: the token
        quota the request path reads has to be the one the plan sells."""
        uow = FakeUow()
        await ChangePlan(uow_factory(uow)).execute(TENANT_ID, PlanTier.GROWTH)

        growth = PLANS[PlanTier.GROWTH]
        assert uow.tenants.tenant.daily_token_quota == growth.daily_token_quota
        assert uow.tenants.tenant.max_documents == growth.max_documents

    @pytest.mark.asyncio
    async def test_subscribing_records_a_billing_line(self) -> None:
        uow = FakeUow()
        await ChangePlan(uow_factory(uow)).execute(
            TENANT_ID, PlanTier.SCALE, reference="ch_test_123"
        )

        line = uow.billing_transactions.items[0]
        assert line.amount_usd == 15.0
        assert line.plan_tier == PlanTier.SCALE.value
        assert line.reference == "ch_test_123"


# --- The catalogue itself ----------------------------------------------------


class TestPlanCatalogue:
    def test_the_free_tier_can_mint_no_keys(self) -> None:
        assert PLANS[PlanTier.FREE].api_access is False
        assert PLANS[PlanTier.FREE].scopes == frozenset()

    def test_agent_api_is_the_line_between_the_ten_and_fifteen_dollar_plans(self) -> None:
        assert PLANS[PlanTier.GROWTH].price_usd == 10.0
        assert PLANS[PlanTier.GROWTH].agent_api is False
        assert PLANS[PlanTier.SCALE].price_usd == 15.0
        assert PLANS[PlanTier.SCALE].agent_api is True

    def test_an_unknown_tier_degrades_to_free_rather_than_raising(self) -> None:
        """Read off a row, so it must never 500 — and must never fail open."""
        assert plan_for("enterprise_platinum").tier is PlanTier.FREE

    def test_tiers_are_ordered_by_capability(self) -> None:
        assert PlanTier.SCALE.at_least(PlanTier.GROWTH)
        assert not PlanTier.STARTER.at_least(PlanTier.SCALE)


# --- The gate at the HTTP boundary -------------------------------------------


class TestScopeEnforcement:
    """The scope check is only worth anything if it is actually mounted on the
    endpoints that spend tokens. These drive real requests through the app so a
    future refactor that drops the dependency fails here rather than in
    production."""

    @staticmethod
    def _client(tier: PlanTier) -> tuple[TestClient, str]:
        """An app whose only account is on `tier`, plus a live key for it."""
        uow = FakeUow(subscription=paid(tier))
        raw = generate_secret_key()
        uow.api_keys.items.append(
            ApiKey(
                tenant_id=TENANT_ID,
                name="integration",
                key_hash=hash_api_key(raw),
                prefix=raw[:12],
                scopes=sorted(s.value for s in plan_for(tier).scopes),
                plan_tier=tier.value,
            )
        )

        container = get_container()
        container.unit_of_work = lambda: uow_factory(uow)  # type: ignore[assignment]
        app = create_app()
        app.dependency_overrides[container_dep] = lambda: container
        return TestClient(app), raw

    def test_a_growth_key_is_refused_the_agent_endpoint(self) -> None:
        client, raw = self._client(PlanTier.GROWTH)
        with client:
            res = client.post(
                f"/api/v1/sessions/{uuid.uuid4()}/agent",
                json={"question": "hello"},
                headers={"X-API-Key": raw},
            )

        assert res.status_code == 403
        # The refusal names the plan that would work.
        assert "Scale" in res.json()["detail"]

    def test_a_scale_key_gets_past_the_scope_gate(self) -> None:
        client, raw = self._client(PlanTier.SCALE)
        with client:
            res = client.post(
                f"/api/v1/sessions/{uuid.uuid4()}/agent",
                json={"question": "hello"},
                headers={"X-API-Key": raw},
            )

        # It fails later, on the nonexistent session — which is the point: the
        # scope gate let it through and the request reached the use case.
        assert res.status_code != 403

    def test_an_unknown_key_is_rejected_without_saying_why(self) -> None:
        client, _ = self._client(PlanTier.SCALE)
        with client:
            res = client.post(
                f"/api/v1/sessions/{uuid.uuid4()}/agent",
                json={"question": "hello"},
                headers={"X-API-Key": "sk_not_a_real_key"},
            )

        assert res.status_code == 401
        assert res.json()["detail"] == "Invalid API key"
