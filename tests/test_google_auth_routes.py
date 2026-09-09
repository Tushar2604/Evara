"""The Google sign-in endpoints, end to end with a stubbed Google.

Everything except Google itself is real here — routing, state signing, the
origin check, the use case, and the page the popup lands on. What this proves
that the unit tests cannot: that a completed consent actually produces a session
in the popup's message, and that none of the failure paths leak one.
"""

from __future__ import annotations

import json
import re
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from src.config.container import get_container
from src.domain.shared.identifiers import TenantId
from src.domain.tenant.entities import Role, Tenant, User
from src.infrastructure.oauth.providers import OAuthBroker, OAuthTokens
from src.interfaces.api.app import create_app
from src.interfaces.api.deps import container_dep

CALLBACK = "/api/v1/auth/google/callback"


class FakeUsers:
    def __init__(self, users: list[User] | None = None) -> None:
        self.items = list(users or [])

    async def get_by_email(self, email: str) -> User | None:
        return next((u for u in self.items if u.email == email), None)

    async def add(self, user: User) -> None:
        self.items.append(user)

    async def touch_login(self, user_id, when) -> None:
        pass


class FakeTenants:
    def __init__(self) -> None:
        self.added: list[Tenant] = []

    async def get_by_slug(self, slug: str):
        return next((t for t in self.added if t.slug == slug), None)

    async def get(self, tenant_id: TenantId):
        return next((t for t in self.added if t.id == tenant_id), None) or Tenant(
            name="x", slug="x", id=tenant_id, is_active=True
        )

    async def add(self, tenant: Tenant) -> None:
        self.added.append(tenant)


class FakeRepo:
    def __init__(self) -> None:
        self.added: list = []

    async def add(self, item) -> None:
        self.added.append(item)

    async def list_resumable(self) -> list:
        # Called by the startup resume sweep, which runs for every TestClient.
        return []


class FakeUow:
    def __init__(self, users: FakeUsers, tenants: FakeTenants) -> None:
        self.users = users
        self.tenants = tenants
        self.chatbots = FakeRepo()
        self.documents = FakeRepo()
        self.committed = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def set_tenant_scope(self, tenant_id) -> None:
        pass

    def collect_event(self, event) -> None:
        pass

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        self.committed += 1


class StubGoogle:
    """Stands in for the Google OAuth client on the container's broker."""

    def __init__(self, profile: dict | None = None, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._profile = profile if profile is not None else {
            "email": "asha@example.com",
            "email_verified": True,
            "name": "Asha Menon",
        }
        self.spec = OAuthBroker(get_container().settings).get("google_login").spec

    def authorize_url(self, state: str) -> str:
        return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}"

    async def exchange_code(self, code: str) -> OAuthTokens:
        if code == "bad-code":
            raise RuntimeError("invalid_grant")
        return OAuthTokens(
            access_token="google-access",
            refresh_token="",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope="openid email profile",
        )

    async def fetch_profile(self, access_token: str) -> dict:
        return self._profile


@pytest.fixture
def api():
    users, tenants = FakeUsers(), FakeTenants()
    container = get_container()
    stub = StubGoogle()

    @asynccontextmanager
    async def fake_uow():
        yield FakeUow(users, tenants)

    container.unit_of_work = fake_uow  # type: ignore[assignment]
    original = container.oauth.get

    def get(provider_id: str):
        return stub if provider_id == "google_login" else original(provider_id)

    container.oauth.get = get  # type: ignore[assignment]

    app = create_app()
    app.dependency_overrides[container_dep] = lambda: container
    with TestClient(app) as client:
        yield client, users, tenants, stub

    container.oauth.get = original  # type: ignore[assignment]


def _post_message(html: str) -> tuple[dict, str]:
    """The (payload, targetOrigin) the callback page posts to its opener.

    Greedy on the payload: it contains a nested `session` object, and a lazy
    match stops at the first inner brace.
    """
    match = re.search(r"postMessage\((\{.*\}), (\".*?\")\)", html, re.S)
    assert match, html[:400]
    return json.loads(match.group(1)), json.loads(match.group(2))


def _message(html: str) -> dict:
    return _post_message(html)[0]


def _target_origin(html: str) -> str:
    return _post_message(html)[1]


def _start(client: TestClient, origin: str = "http://localhost:5173") -> str:
    response = client.get("/api/v1/auth/google/start", headers={"Origin": origin})
    assert response.status_code == 200, response.text
    return response.json()["authorize_url"].split("state=")[1]


class TestProviders:
    def test_google_is_advertised_when_configured(self, api) -> None:
        client, *_ = api
        # The stub reports enabled, which is what the login page keys on.
        assert client.get("/api/v1/auth/providers").json()["google"] is True


class TestSuccess:
    def test_a_new_person_gets_a_session_and_a_workspace(self, api) -> None:
        client, users, tenants, _ = api

        html = client.get(CALLBACK, params={"code": "ok", "state": _start(client)}).text
        message = _message(html)

        assert message["ok"] is True
        assert message["session"]["access_token"]
        assert message["session"]["role"] == Role.OWNER.value
        assert message["email"] == "asha@example.com"
        assert len(tenants.added) == 1
        assert [u.email for u in users.items] == ["asha@example.com"]

    def test_an_existing_person_is_signed_into_their_own_tenant(self, api) -> None:
        client, users, tenants, _ = api
        existing = User(
            email="asha@example.com",
            password_hash="",
            tenant_id=TenantId(uuid.uuid4()),
            role=Role.ADMIN,
        )
        users.items.append(existing)

        message = _message(
            client.get(CALLBACK, params={"code": "ok", "state": _start(client)}).text
        )

        assert message["session"]["tenant_id"] == str(existing.tenant_id)
        assert message["session"]["role"] == Role.ADMIN.value
        assert tenants.added == []  # no second workspace

    def test_the_session_is_posted_to_the_origin_that_started_the_flow(self, api) -> None:
        # The dev-server case: SPA on :5173, API on :8000.
        client, *_ = api
        state = _start(client, "http://localhost:5173")

        html = client.get(CALLBACK, params={"code": "ok", "state": state}).text

        assert _target_origin(html) == "http://localhost:5173"
        assert '"*"' not in html


class TestFailuresLeakNothing:
    def _assert_no_session(self, html: str) -> dict:
        message = _message(html)
        assert message["ok"] is False
        assert "session" not in message
        assert "access_token" not in html
        return message

    def test_a_declined_consent(self, api) -> None:
        client, *_ = api
        html = client.get(CALLBACK, params={"error": "access_denied"}).text
        self._assert_no_session(html)
        assert "cancelled" in html

    def test_a_forged_state(self, api) -> None:
        client, *_ = api
        html = client.get(CALLBACK, params={"code": "ok", "state": "not-a-jwt"}).text
        self._assert_no_session(html)

    def test_a_state_minted_for_a_different_provider(self, api) -> None:
        from src.interfaces.api.oauth_popup import sign_state

        client, *_ = api
        stolen = sign_state("google_calendar", "http://localhost:5173", tenant_id="t")
        html = client.get(CALLBACK, params={"code": "ok", "state": stolen}).text
        self._assert_no_session(html)

    def test_google_refusing_the_code(self, api) -> None:
        client, *_ = api
        html = client.get(CALLBACK, params={"code": "bad-code", "state": _start(client)}).text
        self._assert_no_session(html)

    def test_an_unverified_email(self, api) -> None:
        client, _, tenants, stub = api
        stub._profile = {"email": "asha@example.com", "email_verified": False}

        html = client.get(CALLBACK, params={"code": "ok", "state": _start(client)}).text

        self._assert_no_session(html)
        assert tenants.added == []  # and no account was created
