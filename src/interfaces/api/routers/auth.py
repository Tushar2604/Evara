"""Auth endpoints: register a tenant, log in."""

from __future__ import annotations

import html
import uuid

import jwt
import structlog
from fastapi import APIRouter, HTTPException

from src.application.dtos import RegisterTenantInput
from src.config.settings import get_settings
from src.domain.shared.identifiers import UserId
from src.application.use_cases.authenticate import AuthenticateUser, LoginInput
from src.application.use_cases.register_tenant import RegisterTenant
from src.interfaces.api.deps import ContainerDep
from src.interfaces.api.schemas import (
    AuthProvidersResponse,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
)

router = APIRouter(prefix="/auth", tags=["auth"])

log = structlog.get_logger(__name__)

# Deliberately identical whether or not the address is registered.
_RESET_SENT = (
    "If that email has an account, a reset link is on its way. "
    "The link is valid for a short time."
)


@router.get("/providers", response_model=AuthProvidersResponse)
async def providers(container: ContainerDep) -> AuthProvidersResponse:
    """Which sign-in methods this deployment actually offers.

    Served so the login page can hide a Google button that would only fail —
    a visible button that 400s is worse than no button, and the SPA has no
    other way to know whether the server has an OAuth app registered.
    """
    client = container.oauth.get("google_login")
    return AuthProvidersResponse(google=bool(client and client.enabled))


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(body: RegisterRequest, container: ContainerDep) -> TokenResponse:
    use_case = RegisterTenant(container.unit_of_work(), container.hasher, container.tokens)
    result = await use_case.execute(
        RegisterTenantInput(
            tenant_name=body.tenant_name, owner_email=body.email, password=body.password
        )
    )
    return TokenResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        tenant_id=result.tenant_id,
        user_id=result.user_id,
        role=result.role,
        is_platform_admin=result.is_platform_admin,
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, container: ContainerDep) -> TokenResponse:
    use_case = AuthenticateUser(container.unit_of_work(), container.hasher, container.tokens)
    result = await use_case.execute(LoginInput(email=body.email, password=body.password))
    return TokenResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        tenant_id=result.tenant_id,
        user_id=result.user_id,
        role=result.role,
        is_platform_admin=result.is_platform_admin,
    )


@router.post("/forgot-password", response_model=ForgotPasswordResponse)
async def forgot_password(
    body: ForgotPasswordRequest, container: ContainerDep
) -> ForgotPasswordResponse:
    """Email a reset link.

    Always answers the same way. Saying "no such account" here would turn this
    into a way to discover which addresses are registered, and the caller is
    unauthenticated by definition.
    """
    settings = get_settings()
    async with container.unit_of_work() as uow:
        user = await uow.users.get_by_email(body.email.lower().strip())

    # Only reachable for a real, active account; the response does not change.
    if user is not None and user.is_active:
        token = container.tokens.issue_password_reset(
            user_id=str(user.id), password_hash=user.password_hash
        )
        link = f"{settings.app_base_url.rstrip('/')}/reset-password?token={token}"
        sent = await container.email.send(
            to=user.email,
            subject="Reset your password",
            html=(
                "<p>Someone asked to reset the password for this account.</p>"
                f'<p><a href="{html.escape(link, quote=True)}">Choose a new password</a></p>'
                f"<p>The link expires in {settings.password_reset_ttl_minutes} minutes. "
                "If this wasn't you, ignore this email — nothing has changed.</p>"
            ),
        )
        # Logged (without the token) so an operator can tell a delivery failure
        # from a mistyped address, which the response deliberately cannot.
        log.info("auth.password_reset.requested", user_id=str(user.id), delivered=bool(sent))

    return ForgotPasswordResponse(detail=_RESET_SENT, email_sent=container.email.enabled)


@router.post("/reset-password", response_model=TokenResponse)
async def reset_password(body: ResetPasswordRequest, container: ContainerDep) -> TokenResponse:
    """Set a new password from a reset link, and sign the user straight in.

    Signing in on success is deliberate: the alternative is bouncing someone who
    has just proved control of the mailbox back to a login form to retype the
    password they set two seconds ago.
    """
    invalid = HTTPException(
        status_code=400, detail="This reset link is invalid or has expired. Request a new one."
    )
    try:
        claims = container.tokens.decode_password_reset(body.token)
    except jwt.PyJWTError as exc:
        raise invalid from exc

    async with container.unit_of_work() as uow:
        user = await uow.users.get(UserId(uuid.UUID(claims["sub"])))
        if user is None or not user.is_active:
            raise invalid
        # Ties the token to the password it was issued against, which is what
        # makes it single-use: once the hash changes, every link issued for the
        # old one stops working.
        if not container.tokens.reset_matches_current(claims, user.password_hash):
            raise invalid

        await uow.users.set_password_hash(user.id, container.hasher.hash(body.new_password))
        await uow.commit()

    log.info("auth.password_reset.completed", user_id=str(user.id))
    pair = container.tokens.issue(
        user_id=str(user.id),
        tenant_id=str(user.tenant_id),
        role=user.role.value,
        is_platform_admin=user.is_platform_admin,
    )
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        tenant_id=str(user.tenant_id),
        user_id=str(user.id),
        role=user.role.value,
        is_platform_admin=user.is_platform_admin,
    )
