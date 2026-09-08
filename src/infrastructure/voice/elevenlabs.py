"""ElevenLabs voice cloning + synthesis.

Follows the same opt-in shape as the calendar and email adapters: with no API
key it reports `enabled is False` and every call returns a clear "not
configured" outcome rather than raising. The Clone Voice page still records,
validates, stores, and manages samples in that state — only the cloning step is
unavailable, and it says so.

Never raises: a vendor outage should leave a voice profile marked `failed` with
a readable reason, not 500 the request that created it.
"""

from __future__ import annotations

import httpx
import structlog

from src.config.settings import Settings
from src.infrastructure.http_client import get_client

log = structlog.get_logger(__name__)

_API_BASE = "https://api.elevenlabs.io/v1"
CLONE_TIMEOUT_SECONDS = 120
SYNTH_TIMEOUT_SECONDS = 60

PROVIDER_NAME = "elevenlabs"

_NOT_CONFIGURED = (
    "Voice cloning isn't configured on this server. Add ELEVENLABS_API_KEY to "
    ".env to enable it."
)


class ElevenLabsVoiceCloner:
    def __init__(self, settings: Settings) -> None:
        self._api_key = settings.elevenlabs_api_key
        self._model = settings.elevenlabs_model

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    @property
    def provider(self) -> str:
        return PROVIDER_NAME

    def _headers(self) -> dict[str, str]:
        return {"xi-api-key": self._api_key}

    async def clone(
        self,
        *,
        name: str,
        audio: bytes,
        content_type: str,
        filename: str = "sample",
        description: str = "",
    ) -> tuple[bool, str, str]:
        """Create a voice from one sample. Returns `(ok, voice_id, error)`."""
        if not self.enabled:
            return False, "", _NOT_CONFIGURED

        files = {"files": (filename, audio, content_type or "audio/webm")}
        data = {"name": name}
        if description:
            data["description"] = description

        try:
            client = await get_client("elevenlabs_clone", timeout=CLONE_TIMEOUT_SECONDS)
            resp = await client.post(
                f"{_API_BASE}/voices/add",
                headers=self._headers(),
                data=data,
                files=files,
            )
        except httpx.HTTPError as exc:
            log.warning("voice.clone_error", error=str(exc))
            return False, "", f"Could not reach the voice provider: {exc}"

        if resp.status_code >= 400:
            detail = _error_detail(resp)
            log.warning("voice.clone_rejected", status=resp.status_code, detail=detail)
            return False, "", detail

        try:
            voice_id = str(resp.json().get("voice_id", ""))
        except ValueError:
            return False, "", "The voice provider returned a response we couldn't read."
        if not voice_id:
            return False, "", "The voice provider didn't return a voice id."
        return True, voice_id, ""

    async def synthesize(self, voice_id: str, text: str) -> tuple[bool, bytes, str]:
        """Render `text` in a cloned voice. Returns `(ok, mp3_bytes, error)`."""
        if not self.enabled:
            return False, b"", _NOT_CONFIGURED
        if not voice_id:
            return False, b"", "This voice isn't ready yet."

        try:
            client = await get_client("elevenlabs_synth", timeout=SYNTH_TIMEOUT_SECONDS)
            resp = await client.post(
                f"{_API_BASE}/text-to-speech/{voice_id}",
                headers={**self._headers(), "Accept": "audio/mpeg"},
                json={"text": text, "model_id": self._model},
            )
        except httpx.HTTPError as exc:
            log.warning("voice.synth_error", error=str(exc))
            return False, b"", f"Could not reach the voice provider: {exc}"

        if resp.status_code >= 400:
            return False, b"", _error_detail(resp)
        return True, resp.content, ""

    async def delete(self, voice_id: str) -> None:
        """Best-effort cleanup at the vendor.

        Deliberately silent on failure: the local profile is being deleted
        either way, and blocking that on a vendor error would leave the user
        unable to remove their own recording.
        """
        if not self.enabled or not voice_id:
            return
        try:
            client = await get_client("elevenlabs_synth", timeout=SYNTH_TIMEOUT_SECONDS)
            await client.delete(f"{_API_BASE}/voices/{voice_id}", headers=self._headers())
        except httpx.HTTPError as exc:  # noqa: BLE001
            log.warning("voice.delete_error", voice_id=voice_id, error=str(exc))


def _error_detail(resp: httpx.Response) -> str:
    """ElevenLabs nests the useful message under `detail`, which is far more
    actionable than the status line ('sample too short', 'quota exceeded')."""
    try:
        data = resp.json()
    except ValueError:
        return f"Voice provider returned HTTP {resp.status_code}."
    detail = data.get("detail", data)
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("status") or detail)[:500]
    return str(detail)[:500]
