"""External-service ports.

Everything the core needs from the outside world — embeddings, LLMs, storage,
hashing, tokens, events, parsing — expressed as Protocols. Infrastructure
provides concrete adapters (Gemini, Groq, R2, Argon2, JWT, ...).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class Embedder(Protocol):
    """Turns text into vectors. Free tier: Gemini text-embedding-004."""

    @property
    def dim(self) -> int: ...
    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_query(self, text: str) -> list[float]: ...


@dataclass
class LLMResult:
    text: str
    tokens_used: int
    provider: str
    model: str


@runtime_checkable
class LLMProvider(Protocol):
    """A single generation backend (OpenAI, Groq, Gemini, or Ollama)."""

    name: str

    async def generate(self, system: str, user: str) -> LLMResult: ...
    def stream(
        self,
        system: str,
        user: str,
        on_provider: Callable[[str], None] | None = None,
    ) -> AsyncIterator[str]:
        """Stream tokens. `on_provider`, if given, is called once with the name
        of the backend that actually served the request (so the streaming path
        can record provider despite failover)."""
        ...
    async def describe_image(self, data: bytes, content_type: str) -> str:
        """Transcribe an image into text for the ingestion pipeline (a chatbot
        attachment that's a screenshot, scan, or photo rather than a PDF/DOCX).
        Not every backend is multimodal — a provider that isn't should raise
        NotImplementedError so a failover chain can try the next one."""
        ...


@runtime_checkable
class ObjectStorage(Protocol):
    """R2 in prod; local disk fallback in dev."""

    async def presign_put(self, key: str, content_type: str, max_bytes: int) -> str: ...
    async def get_bytes(self, key: str) -> bytes: ...
    async def put_bytes(self, key: str, data: bytes, content_type: str) -> None: ...
    async def delete(self, key: str) -> None: ...


@runtime_checkable
class PasswordHasher(Protocol):
    def hash(self, plain: str) -> str: ...
    def verify(self, plain: str, hashed: str) -> bool: ...


@dataclass
class TokenPair:
    access_token: str
    refresh_token: str


@runtime_checkable
class TokenService(Protocol):
    def issue(
        self, *, user_id: str, tenant_id: str, role: str, is_platform_admin: bool = False
    ) -> TokenPair: ...
    def decode(self, token: str) -> dict: ...


@runtime_checkable
class EventBus(Protocol):
    async def publish(self, event: object) -> None: ...


@runtime_checkable
class DocumentParser(Protocol):
    """Extract plain text from an uploaded file by content type."""

    def supports(self, content_type: str, filename: str) -> bool: ...
    async def extract_text(self, data: bytes, content_type: str, filename: str) -> str: ...


@runtime_checkable
class Chunker(Protocol):
    def chunk(self, text: str) -> list[str]: ...
