"""Resend email adapter — candidate interview-invite delivery.

Raw REST call via httpx (Resend's API is a single simple POST, no SDK needed),
same resilience pattern as the LLM providers. If RESEND_API_KEY is unset,
`enabled` is False and callers skip sending entirely rather than erroring —
scheduling an interview never hard-depends on this.
"""

from __future__ import annotations

import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from src.config.settings import Settings
from src.infrastructure.http_client import get_client
from src.infrastructure.llm.resilience import is_transient

log = structlog.get_logger(__name__)

_API_URL = "https://api.resend.com/emails"

# Retries only a blip, not a permanent failure (bad address, invalid key) —
# see the identical note in infrastructure/calendar/google.py.
_resend_retry = retry(
    retry=retry_if_exception(is_transient),
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=10),
    reraise=True,
)


class ResendEmailSender:
    def __init__(self, settings: Settings) -> None:
        self._api_key = settings.resend_api_key
        self._from_email = settings.resend_from_email

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    @_resend_retry
    async def send(self, *, to: str, subject: str, html: str) -> bool:
        if not self.enabled:
            return False
        client = await get_client("resend", timeout=15)
        resp = await client.post(
            _API_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={"from": self._from_email, "to": [to], "subject": subject, "html": html},
        )
        if resp.status_code >= 400:
            log.warning("resend.send_failed", status=resp.status_code, body=resp.text[:500])
            return False
        return True
