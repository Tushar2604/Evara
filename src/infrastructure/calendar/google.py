"""Google Calendar OAuth + event creation.

Raw REST calls via httpx (no google-api-python-client dependency), mirroring
the style already used for Gemini — see infrastructure/llm/embeddings.py.
Per-tenant tokens live in GoogleOAuthConnection (application/ports/
repositories.py); this client never persists anything itself — callers own
persistence via the GoogleConnectionRepository port.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from src.application.ports.repositories import GoogleOAuthConnection
from src.config.settings import Settings
from src.infrastructure.http_client import get_client
from src.infrastructure.llm.resilience import is_transient

# Retries only a blip (dropped connection, 503, timeout) — never a permanent
# failure like a revoked grant or bad request, which a blanket retry used to
# hit 3 times with up to ~10s of backoff apiece before finally raising. This
# call runs inline in the booking/scheduling request path, so that cost was
# paid by the user waiting on the response, not just by the failing call.
_calendar_retry = retry(
    retry=retry_if_exception(is_transient),
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=10),
    reraise=True,
)

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_CALENDAR_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
_SCOPE = (
    "https://www.googleapis.com/auth/calendar.events "
    "https://www.googleapis.com/auth/userinfo.email"
)


class GoogleCalendarClient:
    def __init__(self, settings: Settings) -> None:
        self._client_id = settings.google_oauth_client_id
        self._client_secret = settings.google_oauth_client_secret
        self._redirect_uri = settings.google_oauth_redirect_uri

    @property
    def enabled(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def authorize_url(self, state: str) -> str:
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": _SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        return f"{_AUTH_URL}?{urlencode(params)}"

    @_calendar_retry
    async def exchange_code(self, code: str) -> dict:
        """One-time authorization-code exchange. Returns the raw token
        response (access_token, refresh_token, expires_in, scope)."""
        client = await get_client("google_calendar", timeout=15)
        resp = await client.post(
            _TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": self._redirect_uri,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def fetch_email(self, access_token: str) -> str:
        client = await get_client("google_calendar", timeout=15)
        resp = await client.get(
            _USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
        resp.raise_for_status()
        return resp.json().get("email", "")

    @_calendar_retry
    async def _refresh(self, connection: GoogleOAuthConnection) -> GoogleOAuthConnection:
        client = await get_client("google_calendar", timeout=15)
        resp = await client.post(
            _TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": connection.refresh_token,
                "grant_type": "refresh_token",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        connection.access_token = data["access_token"]
        connection.expires_at = datetime.now(UTC) + timedelta(seconds=data.get("expires_in", 3600))
        return connection

    async def ensure_fresh(self, connection: GoogleOAuthConnection) -> GoogleOAuthConnection:
        """Refreshes the access token in place if it's expired/near-expiry.
        Callers should re-persist the connection when this actually refreshed
        (compare expires_at, or just always upsert — it's a cheap write)."""
        if connection.expires_at <= datetime.now(UTC) + timedelta(seconds=60):
            return await self._refresh(connection)
        return connection

    @_calendar_retry
    async def create_event(
        self,
        connection: GoogleOAuthConnection,
        *,
        summary: str,
        description: str,
        start_time: datetime,
        duration_minutes: int,
        attendee_email: str,
    ) -> tuple[str, str]:
        """Creates the event on the connected tenant's primary calendar,
        inviting the candidate. Returns (event_id, html_link)."""
        end_time = start_time + timedelta(minutes=duration_minutes)
        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start_time.isoformat()},
            "end": {"dateTime": end_time.isoformat()},
            "attendees": [{"email": attendee_email}],
        }
        client = await get_client("google_calendar", timeout=15)
        resp = await client.post(
            _CALENDAR_EVENTS_URL,
            json=body,
            params={"sendUpdates": "all"},
            headers={"Authorization": f"Bearer {connection.access_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["id"], data.get("htmlLink", "")
