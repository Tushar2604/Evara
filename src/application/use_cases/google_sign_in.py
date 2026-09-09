"""Sign in (or sign up) with a Google account.

Two outcomes, decided entirely by whether the email already has an account:
an existing user is logged in, and an unknown one gets a brand-new workspace.
There is no third "link your accounts?" screen — the email *is* the identity,
and Google has already proved the person controls it.

That last clause is the whole security model, so it is enforced rather than
assumed: Google reports `email_verified`, and a sign-in whose email is not
verified is refused outright. Without that check, anyone who can create a Google
account claiming an address could take over the existing account on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from src.application.dtos import AuthOutput
from src.application.ports.repositories import UnitOfWork
from src.application.ports.services import TokenService
from src.application.use_cases.provisioning import (
    provision_tenant,
    unique_slug,
    workspace_name_for,
)
from src.domain.shared.errors import PermissionDeniedError


@dataclass
class GoogleProfile:
    """The identity Google returned, narrowed to what sign-in actually uses."""

    email: str
    email_verified: bool
    full_name: str = ""

    @classmethod
    def from_userinfo(cls, payload: dict) -> GoogleProfile:
        # `verified_email` is the v2 userinfo spelling, `email_verified` the
        # OpenID one. Google returns different keys depending on the endpoint,
        # and defaulting to False means an unrecognised shape fails closed.
        verified = payload.get("email_verified", payload.get("verified_email", False))
        return cls(
            email=str(payload.get("email", "")).strip().lower(),
            email_verified=bool(verified),
            full_name=str(payload.get("name", "")).strip(),
        )


class GoogleSignIn:
    """Turn a verified Google profile into a session."""

    def __init__(self, uow: UnitOfWork, tokens: TokenService) -> None:
        self._uow = uow
        self._tokens = tokens

    async def execute(self, profile: GoogleProfile) -> AuthOutput:
        if not profile.email:
            raise PermissionDeniedError("Google did not return an email address.")
        if not profile.email_verified:
            raise PermissionDeniedError(
                "That Google account's email address isn't verified, so it can't be "
                "used to sign in."
            )

        async with self._uow as uow:
            user = await uow.users.get_by_email(profile.email)

            if user is not None:
                if not user.is_active:
                    raise PermissionDeniedError("This account has been deactivated.")
                tenant = await uow.tenants.get(user.tenant_id)
                if tenant is None or not tenant.is_active:
                    raise PermissionDeniedError(
                        "This workspace has been suspended. Contact support for help."
                    )
                tenant_id, user_id, role = user.tenant_id, user.id, user.role.value
                is_platform_admin = user.is_platform_admin
                await uow.users.touch_login(user.id, datetime.now(UTC))
            else:
                # New person: give them a workspace. `password_hash=""` marks the
                # account as SSO-only — `AuthenticateUser` refuses to verify a
                # password against it, so an empty password can never log in.
                name = workspace_name_for(profile.email, profile.full_name)
                tenant, created = await provision_tenant(
                    uow,
                    tenant_name=name,
                    owner_email=profile.email,
                    password_hash="",
                    slug=await unique_slug(uow, name),
                )
                tenant_id, user_id, role = tenant.id, created.id, created.role.value
                is_platform_admin = False

            await uow.commit()

        pair = self._tokens.issue(
            user_id=str(user_id),
            tenant_id=str(tenant_id),
            role=role,
            is_platform_admin=is_platform_admin,
        )
        return AuthOutput(
            access_token=pair.access_token,
            refresh_token=pair.refresh_token,
            tenant_id=tenant_id,
            user_id=user_id,
            role=role,
            is_platform_admin=is_platform_admin,
        )
