"""Authenticate a user and issue a JWT pair."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, EmailStr

from src.application.dtos import AuthOutput
from src.application.ports.repositories import UnitOfWork
from src.application.ports.services import PasswordHasher, TokenService
from src.domain.shared.errors import PermissionDeniedError


class LoginInput(BaseModel):
    email: EmailStr
    password: str


class AuthenticateUser:
    def __init__(self, uow: UnitOfWork, hasher: PasswordHasher, tokens: TokenService) -> None:
        self._uow = uow
        self._hasher = hasher
        self._tokens = tokens

    async def execute(self, data: LoginInput) -> AuthOutput:
        async with self._uow as uow:
            user = await uow.users.get_by_email(data.email)
            if user is None or not user.is_active:
                raise PermissionDeniedError("Invalid credentials.")
            tenant = await uow.tenants.get(user.tenant_id)
            if tenant is None or not tenant.is_active:
                # Distinct message from "Invalid credentials": the password may be
                # right, but the workspace itself has been suspended (see the
                # Super Admin panel's tenant-status control), and the person
                # signing in should be told that rather than told to retype it.
                raise PermissionDeniedError(
                    "This workspace has been suspended. Contact support for help."
                )
            if not user.password_hash:
                # An SSO-only account (created by Google sign-in) has no password
                # to check. Refusing explicitly matters: it stops any hasher whose
                # verify() is lenient about empty input from admitting a caller
                # who supplied nothing, and it tells the person which button to
                # press instead of leaving them retrying a password they never set.
                raise PermissionDeniedError(
                    "This account signs in with Google — use Continue with Google."
                )
            if not self._hasher.verify(data.password, user.password_hash):
                raise PermissionDeniedError("Invalid credentials.")

            await uow.users.touch_login(user.id, datetime.now(UTC))
            await uow.commit()

        pair = self._tokens.issue(
            user_id=str(user.id),
            tenant_id=str(user.tenant_id),
            role=user.role.value,
            is_platform_admin=user.is_platform_admin,
        )
        return AuthOutput(
            access_token=pair.access_token,
            refresh_token=pair.refresh_token,
            tenant_id=user.tenant_id,
            user_id=user.id,
            role=user.role.value,
            is_platform_admin=user.is_platform_admin,
        )
