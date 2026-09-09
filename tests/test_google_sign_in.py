"""Signing in with Google.

The rule that matters: the email is the identity, so an existing account is
entered by anyone Google says controls that address. Every test here is about
the boundary of that rule — who gets in, who gets a new workspace, and who is
turned away.
"""

from __future__ import annotations

import uuid

import pytest
from src.application.use_cases.google_sign_in import GoogleProfile, GoogleSignIn
from src.application.use_cases.provisioning import slugify, workspace_name_for
from src.domain.shared.errors import PermissionDeniedError
from src.domain.shared.identifiers import TenantId
from src.domain.tenant.entities import Role, Tenant, User


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
    def __init__(self, taken: set[str] | None = None) -> None:
        self.taken = set(taken or ())
        self.added: list[Tenant] = []
        # Every tenant this fake is asked about is active unless a test puts
        # its id here — that's the one knob `test_a_suspended_workspace_is_refused`
        # (if such a test exists) would need.
        self.suspended: set[TenantId] = set()

    async def get_by_slug(self, slug: str):
        return object() if slug in self.taken else None

    async def get(self, tenant_id: TenantId):
        return Tenant(
            name="x", slug="x", id=tenant_id, is_active=tenant_id not in self.suspended
        )

    async def add(self, tenant: Tenant) -> None:
        self.taken.add(tenant.slug)
        self.added.append(tenant)


class FakeChatbots:
    def __init__(self) -> None:
        self.added: list = []

    async def add(self, bot) -> None:
        self.added.append(bot)


class FakeUow:
    def __init__(self, users: FakeUsers, tenants: FakeTenants) -> None:
        self.users = users
        self.tenants = tenants
        self.chatbots = FakeChatbots()
        self.events: list = []
        self.committed = 0
        self.scoped_to: TenantId | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def set_tenant_scope(self, tenant_id) -> None:
        self.scoped_to = tenant_id

    def collect_event(self, event) -> None:
        self.events.append(event)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        self.committed += 1


class FakeTokens:
    def issue(self, *, user_id: str, tenant_id: str, role: str, is_platform_admin: bool = False):
        from src.application.ports.services import TokenPair

        return TokenPair(access_token=f"acc-{user_id}", refresh_token=f"ref-{user_id}")


def _existing(email: str, *, active: bool = True, password_hash: str = "argon2$x") -> User:
    return User(
        email=email,
        password_hash=password_hash,
        tenant_id=TenantId(uuid.uuid4()),
        role=Role.OWNER,
        is_active=active,
    )


def _verified(email: str = "asha@example.com", name: str = "Asha Menon") -> GoogleProfile:
    return GoogleProfile(email=email, email_verified=True, full_name=name)


async def _run(profile: GoogleProfile, uow: FakeUow):
    return await GoogleSignIn(uow, FakeTokens()).execute(profile)


class TestExistingAccount:
    async def test_a_known_email_is_signed_in(self) -> None:
        user = _existing("asha@example.com")
        uow = FakeUow(FakeUsers([user]), FakeTenants())

        result = await _run(_verified(), uow)

        assert result.user_id == user.id
        assert result.tenant_id == user.tenant_id
        # No new workspace: they already had one.
        assert uow.tenants.added == []

    async def test_a_deactivated_account_is_refused(self) -> None:
        uow = FakeUow(FakeUsers([_existing("asha@example.com", active=False)]), FakeTenants())

        with pytest.raises(PermissionDeniedError, match="deactivated"):
            await _run(_verified(), uow)

    async def test_an_sso_only_account_signs_in_again(self) -> None:
        # Created by a previous Google sign-in, so it has no password hash.
        user = _existing("asha@example.com", password_hash="")
        uow = FakeUow(FakeUsers([user]), FakeTenants())

        assert (await _run(_verified(), uow)).user_id == user.id


class TestNewAccount:
    async def test_an_unknown_email_gets_a_workspace(self) -> None:
        uow = FakeUow(FakeUsers(), FakeTenants())

        result = await _run(_verified(), uow)

        assert len(uow.tenants.added) == 1
        assert result.role == Role.OWNER.value
        assert uow.committed == 1

    async def test_the_new_workspace_gets_a_default_assistant(self) -> None:
        # Same as password sign-up — otherwise a Google tenant lands on an empty
        # dashboard and every "getting started" path dead-ends.
        uow = FakeUow(FakeUsers(), FakeTenants())
        await _run(_verified(), uow)
        assert [b.name for b in uow.chatbots.added] == ["Default Assistant"]

    async def test_the_new_user_has_no_password(self) -> None:
        # The marker that makes AuthenticateUser refuse a password login.
        uow = FakeUow(FakeUsers(), FakeTenants())
        await _run(_verified(), uow)
        assert uow.users.items[0].password_hash == ""

    async def test_a_taken_workspace_name_does_not_block_sign_up(self) -> None:
        # Nobody typed this name, so a clash with an unrelated tenant must not
        # surface as an error — it gets a suffix instead.
        taken = {slugify(workspace_name_for("asha@example.com", "Asha Menon"))}
        uow = FakeUow(FakeUsers(), FakeTenants(taken))

        await _run(_verified(), uow)

        assert len(uow.tenants.added) == 1
        assert uow.tenants.added[0].slug not in taken

    async def test_provisioning_is_scoped_to_the_new_tenant(self) -> None:
        uow = FakeUow(FakeUsers(), FakeTenants())
        result = await _run(_verified(), uow)
        assert uow.scoped_to == result.tenant_id


class TestRefusals:
    async def test_an_unverified_email_cannot_sign_in(self) -> None:
        """The account-takeover guard.

        Without it, anyone who can create a Google account claiming an address
        could walk into the existing account on that address.
        """
        uow = FakeUow(FakeUsers([_existing("asha@example.com")]), FakeTenants())
        profile = GoogleProfile(email="asha@example.com", email_verified=False)

        with pytest.raises(PermissionDeniedError, match="isn't verified"):
            await _run(profile, uow)

    async def test_an_unverified_email_cannot_create_an_account_either(self) -> None:
        uow = FakeUow(FakeUsers(), FakeTenants())
        profile = GoogleProfile(email="new@example.com", email_verified=False)

        with pytest.raises(PermissionDeniedError):
            await _run(profile, uow)
        assert uow.tenants.added == []

    async def test_a_missing_email_is_refused(self) -> None:
        uow = FakeUow(FakeUsers(), FakeTenants())
        with pytest.raises(PermissionDeniedError, match="did not return an email"):
            await _run(GoogleProfile(email="", email_verified=True), uow)


class TestProfileParsing:
    @pytest.mark.parametrize(
        "payload",
        [
            {"email": "a@b.com", "email_verified": True},
            {"email": "a@b.com", "verified_email": True},
        ],
        ids=["openid-spelling", "userinfo-v2-spelling"],
    )
    def test_both_google_spellings_of_verified_are_understood(self, payload: dict) -> None:
        assert GoogleProfile.from_userinfo(payload).email_verified is True

    def test_an_unrecognised_payload_fails_closed(self) -> None:
        # No verification key at all must mean "not verified", never "assume yes".
        assert GoogleProfile.from_userinfo({"email": "a@b.com"}).email_verified is False

    def test_the_email_is_normalised(self) -> None:
        # Google may return mixed case; the account lookup is exact-match.
        profile = GoogleProfile.from_userinfo(
            {"email": "  Asha@Example.COM ", "email_verified": True}
        )
        assert profile.email == "asha@example.com"


class TestWorkspaceNaming:
    def test_a_real_name_is_preferred(self) -> None:
        assert workspace_name_for("asha@example.com", "Asha Menon") == "Asha Menon's Workspace"

    def test_the_email_local_part_is_the_fallback(self) -> None:
        assert workspace_name_for("asha.menon@example.com", "") == "Asha Menon's Workspace"
