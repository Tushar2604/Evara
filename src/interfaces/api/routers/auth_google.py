"""Sign in with Google.

Same popup choreography as connecting an integration, and deliberately so — but
what comes back is a session rather than a stored token, which raises the stakes
on two things:

**Where the result is delivered.** The callback posts an access token to the
opener, so it goes only to an origin this deployment serves, verified when the
flow starts and carried in the signed state (see `oauth_popup.py`). A `"*"`
target here would hand a session to any page that opened the popup.

**Tokens never touch the URL.** A redirect carrying `?access_token=` would land
in browser history, the referrer header, and every proxy log in between.
`postMessage` keeps it out of all three.

The endpoints are unauthenticated by definition — this *is* how you authenticate.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from src.application.use_cases.google_sign_in import GoogleProfile, GoogleSignIn
from src.domain.shared.errors import PermissionDeniedError
from src.interfaces.api.deps import ContainerDep
from src.interfaces.api.oauth_popup import (
    popup_result_page,
    resolve_origin,
    sign_state,
    state_origin,
    verify_state,
)
from src.interfaces.api.schemas import OAuthStartResponse

router = APIRouter(prefix="/auth/google", tags=["auth"])

PROVIDER = "google_login"


def _result(ok: bool, message: str, origin: str, payload: dict | None = None) -> HTMLResponse:
    return popup_result_page(
        ok=ok,
        message=message,
        origin=origin,
        payload={"provider": PROVIDER, **(payload or {})},
        title_ok="Signed in",
        title_failed="Sign-in failed",
    )


@router.get("/start", response_model=OAuthStartResponse)
async def start(request: Request, container: ContainerDep) -> OAuthStartResponse:
    """The Google consent URL. Fetched rather than navigated to, so the SPA can
    open it in a popup and keep the page it is on."""
    client = container.oauth.get(PROVIDER)
    if client is None or not client.enabled:
        raise HTTPException(
            status_code=400,
            detail=(
                "Google sign-in isn't configured on this server. An administrator "
                "needs to set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET."
            ),
        )
    origin = resolve_origin(request.headers.get("origin"))
    return OAuthStartResponse(authorize_url=client.authorize_url(sign_state(PROVIDER, origin)))


@router.get("/callback")
async def callback(
    container: ContainerDep,
    code: str | None = None,
    state: str = "",
    error: str | None = None,
) -> HTMLResponse:
    """Where Google sends the popup back.

    Always returns a page, never raises: this window is the person's only
    feedback, and a raw API error body rendered in a popup tells them nothing
    about what to do next.
    """
    client = container.oauth.get(PROVIDER)
    fallback_origin = resolve_origin(None)
    if client is None or not client.enabled:
        return _result(False, "Google sign-in isn't configured on this server.", fallback_origin)

    if error or not code:
        return _result(
            False,
            "Sign-in was cancelled, so nothing happened. You can try again any time.",
            fallback_origin,
        )

    try:
        payload = verify_state(state, PROVIDER)
    except HTTPException as exc:
        return _result(False, str(exc.detail), fallback_origin)
    origin = state_origin(payload)

    try:
        tokens = await client.exchange_code(code)
        profile = GoogleProfile.from_userinfo(await client.fetch_profile(tokens.access_token))
    except Exception:  # noqa: BLE001 — the vendor's failure, shown as one
        return _result(
            False, "Google refused the sign-in. Please try again.", origin
        )

    try:
        result = await GoogleSignIn(container.unit_of_work(), container.tokens).execute(profile)
    except PermissionDeniedError as exc:
        return _result(False, str(exc), origin)

    # The session itself. Delivered by postMessage to the verified origin, so it
    # never enters the URL, history, or a referrer header.
    return _result(
        True,
        f"Signed in as {profile.email}.",
        origin,
        {
            "session": {
                "access_token": result.access_token,
                "refresh_token": result.refresh_token,
                "token_type": "bearer",
                "tenant_id": str(result.tenant_id),
                "user_id": str(result.user_id),
                "role": result.role,
                "is_platform_admin": result.is_platform_admin,
            },
            "email": profile.email,
        },
    )
