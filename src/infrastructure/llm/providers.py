"""Generation providers (OpenAI, Groq, Gemini, local Ollama) + a failover router.

The router is the key resilience feature: when a provider rate-limits or errors,
it transparently fails over to the next backend in an ordered chain (by default
OpenAI -> Groq -> Gemini), so a user question still gets answered. Ollama runs
models locally (no API key, no quota), which makes it a good primary for
offline/private deployments or a free fallback.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable

import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from src.application.ports.services import LLMResult
from src.config import Settings
from src.infrastructure.llm.resilience import (
    Bulkhead,
    CircuitBreaker,
    is_rate_limited,
    is_transient,
    should_trip_circuit,
)

log = structlog.get_logger(__name__)


def _worth_retrying_here(exc: BaseException) -> bool:
    """Should this failure be retried on the SAME provider?

    Only a transient blip — a dropped connection, a 503 — is. A rate limit
    explicitly is not: retrying it adds load to a backend that just asked us to
    slow down, and it delays the failover to a provider that would have
    answered immediately. With several accounts configured, routing around a
    429 is strictly better than waiting it out.

    This is the single most important behaviour change for concurrency. The old
    blanket `@retry(stop_after_attempt(3), wait_exponential(min=1, max=10))`
    meant a throttled request burned 3-10 seconds on the throttled provider
    before trying the next one, so a burst of traffic queued up behind the one
    backend least able to serve it.
    """
    return is_transient(exc) and not is_rate_limited(exc)


# One quick retry for a genuine blip, then hand off to the failover chain.
_provider_retry = retry(
    retry=retry_if_exception(_worth_retrying_here),
    stop=stop_after_attempt(2),
    wait=wait_exponential(min=0.5, max=4),
    reraise=True,
)


def _estimate_tokens(*parts: str) -> int:
    return max(1, sum(len(p) for p in parts) // 4)


# Shared across every vision-capable provider so a screenshot/scan/photo
# attachment produces real reference material, not a chatty image caption.
_IMAGE_EXTRACTION_PROMPT = (
    "Transcribe this image into plain text reference material for a knowledge "
    "base. Extract all readable text verbatim, describe any tables, charts, or "
    "diagrams factually (their structure and key values), and note any other "
    "important visual facts. Be thorough and literal — do not summarize, add "
    "commentary, or invent anything not actually visible in the image."
)


class OpenAIProvider:
    name = "openai"

    def __init__(self, settings: Settings) -> None:
        self._model = settings.openai_model
        self._temperature = settings.llm_temperature
        self._api_key = settings.openai_api_key
        self._base_url = settings.openai_base_url or None
        self._timeout = settings.llm_request_timeout_seconds
        self._client = None

    def _ensure(self):  # type: ignore[no-untyped-def]
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=self._api_key, base_url=self._base_url, timeout=self._timeout
            )
        return self._client

    @_provider_retry
    async def generate(self, system: str, user: str) -> LLMResult:
        client = self._ensure()
        resp = await client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=self._temperature,
        )
        text = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        tokens = getattr(usage, "total_tokens", None) or _estimate_tokens(system, user, text)
        return LLMResult(text=text, tokens_used=tokens, provider=self.name, model=self._model)

    async def stream(
        self, system: str, user: str, on_provider: Callable[[str], None] | None = None
    ) -> AsyncIterator[str]:
        client = self._ensure()
        if on_provider:
            on_provider(self.name)
        stream = await client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=self._temperature,
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    @_provider_retry
    async def describe_image(self, data: bytes, content_type: str) -> str:
        import base64

        client = self._ensure()
        b64 = base64.b64encode(data).decode("ascii")
        resp = await client.chat.completions.create(
            model=self._model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _IMAGE_EXTRACTION_PROMPT},
                        {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{b64}"}},
                    ],
                },
            ],
        )
        return resp.choices[0].message.content or ""


class GroqProvider:
    name = "groq"

    def __init__(self, settings: Settings) -> None:
        self._model = settings.groq_model
        self._temperature = settings.llm_temperature
        self._api_key = settings.groq_api_key
        self._timeout = settings.llm_request_timeout_seconds
        self._client = None

    def _ensure(self):  # type: ignore[no-untyped-def]
        if self._client is None:
            from groq import AsyncGroq

            self._client = AsyncGroq(api_key=self._api_key, timeout=self._timeout)
        return self._client

    @_provider_retry
    async def generate(self, system: str, user: str) -> LLMResult:
        client = self._ensure()
        resp = await client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=self._temperature,
        )
        text = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        tokens = getattr(usage, "total_tokens", None) or _estimate_tokens(system, user, text)
        return LLMResult(text=text, tokens_used=tokens, provider=self.name, model=self._model)

    async def stream(
        self, system: str, user: str, on_provider: Callable[[str], None] | None = None
    ) -> AsyncIterator[str]:
        client = self._ensure()
        if on_provider:
            on_provider(self.name)
        stream = await client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=self._temperature,
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    async def describe_image(self, data: bytes, content_type: str) -> str:
        raise NotImplementedError("Groq does not support image extraction in this setup.")


class GeminiProvider:
    name = "gemini"

    def __init__(self, settings: Settings) -> None:
        self._model = settings.gemini_model
        self._temperature = settings.llm_temperature
        self._api_key = settings.gemini_api_key
        self._timeout_ms = int(settings.llm_request_timeout_seconds * 1000)
        self._configured = False

    def _ensure(self):  # type: ignore[no-untyped-def]
        if not self._configured:
            from google import genai
            from google.genai import types

            self._client = genai.Client(
                api_key=self._api_key,
                http_options=types.HttpOptions(timeout=self._timeout_ms),
            )
            self._configured = True
        return self._client

    @_provider_retry
    async def generate(self, system: str, user: str) -> LLMResult:
        client = self._ensure()

        def _call() -> str:
            from google.genai import types

            response = client.models.generate_content(
                model=self._model,
                contents=user,
                config=types.GenerateContentConfig(
                    system_instruction=system, temperature=self._temperature
                ),
            )
            return response.text

        text = await asyncio.to_thread(_call)
        tokens = _estimate_tokens(system, user, text)
        return LLMResult(text=text, tokens_used=tokens, provider=self.name, model=self._model)

    async def stream(
        self, system: str, user: str, on_provider: Callable[[str], None] | None = None
    ) -> AsyncIterator[str]:
        client = self._ensure()
        if on_provider:
            on_provider(self.name)

        from google.genai import types

        def _start():  # type: ignore[no-untyped-def]
            return client.models.generate_content_stream(
                model=self._model,
                contents=user,
                config=types.GenerateContentConfig(
                    system_instruction=system, temperature=self._temperature
                ),
            )

        stream = await asyncio.to_thread(_start)
        for chunk in stream:
            if chunk.text:
                yield chunk.text

    @_provider_retry
    async def describe_image(self, data: bytes, content_type: str) -> str:
        client = self._ensure()

        def _call() -> str:
            from google.genai import types

            response = client.models.generate_content(
                model=self._model,
                contents=[
                    types.Part.from_bytes(data=data, mime_type=content_type),
                    _IMAGE_EXTRACTION_PROMPT,
                ],
            )
            return response.text

        return await asyncio.to_thread(_call)


class OllamaProvider:
    """Local generation via an Ollama server (default qwen2.5).

    Talks to Ollama's OpenAI-compatible Chat Completions endpoint over httpx,
    so it needs no extra SDK. No API key and no rate limits — the only cost is
    the local box, which makes it a natural primary for private/offline setups.
    """

    name = "ollama"

    def __init__(self, settings: Settings) -> None:
        self._model = settings.ollama_model
        self._temperature = settings.llm_temperature
        self._base_url = settings.ollama_base_url.rstrip("/")
        self._client = None

    def _ensure(self):  # type: ignore[no-untyped-def]
        if self._client is None:
            import httpx

            # Generous timeout: local models can be slow to load on first call.
            self._client = httpx.AsyncClient(
                base_url=f"{self._base_url}/v1", timeout=httpx.Timeout(120.0)
            )
        return self._client

    @_provider_retry
    async def generate(self, system: str, user: str) -> LLMResult:
        client = self._ensure()
        resp = await client.post(
            "/chat/completions",
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "temperature": self._temperature,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage") or {}
        tokens = usage.get("total_tokens") or _estimate_tokens(system, user, text)
        return LLMResult(text=text, tokens_used=tokens, provider=self.name, model=self._model)

    async def stream(
        self, system: str, user: str, on_provider: Callable[[str], None] | None = None
    ) -> AsyncIterator[str]:
        import json

        client = self._ensure()
        if on_provider:
            on_provider(self.name)
        async with client.stream(
            "POST",
            "/chat/completions",
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": True,
                "temperature": self._temperature,
            },
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[len("data: ") :]
                if payload.strip() == "[DONE]":
                    break
                delta = json.loads(payload)["choices"][0]["delta"].get("content")
                if delta:
                    yield delta

    async def describe_image(self, data: bytes, content_type: str) -> str:
        raise NotImplementedError(
            "Ollama vision support depends on the locally pulled model — not assumed here."
        )


class FailoverLLM:
    """Tries each provider in order, degrading to the next one on failure.

    Under concurrent traffic the ordering alone is not enough — see
    `src/infrastructure/llm/resilience.py` for why. Three things are layered on
    top of the plain loop:

      * a **circuit breaker** per provider, so a backend that is rate-limiting
        or down is skipped outright instead of being re-probed (and re-retried)
        by every concurrent request;
      * a **bulkhead** per provider, capping in-flight calls so a traffic burst
        stays inside each account's quota rather than self-inflicting the very
        429s the chain exists to survive;
      * **rate-limit-aware routing**: a 429 fails over immediately rather than
        being slept on, because with several keys configured there is a healthy
        account one hop away.

    A request that finds every breaker open still tries the chain rather than
    failing fast: a stale breaker must degrade latency, never turn a working
    provider into a hard outage.
    """

    name = "failover"

    def __init__(
        self,
        providers,  # type: ignore[no-untyped-def]
        *,
        breaker_threshold: int = 5,
        breaker_cooldown_seconds: float = 30.0,
        max_concurrency_per_provider: int = 16,
    ) -> None:
        providers = list(providers)
        if not providers:
            raise ValueError("FailoverLLM requires at least one provider")
        self._providers = providers
        self._breakers = {
            p.name: CircuitBreaker(
                threshold=breaker_threshold, cooldown_seconds=breaker_cooldown_seconds
            )
            for p in providers
        }
        self._bulkheads = {
            p.name: Bulkhead(max_concurrency_per_provider) for p in providers
        }

    # -- routing helpers --

    def _skip(self, provider_name: str, respect_breakers: bool) -> bool:
        return respect_breakers and self._breakers[provider_name].is_open

    def _record(self, provider_name: str, exc: BaseException | None) -> None:
        breaker = self._breakers[provider_name]
        if exc is None:
            breaker.record_success()
        elif should_trip_circuit(exc):
            breaker.record_failure(provider_name)

    async def probe(self, *, timeout_seconds: float = 20.0) -> dict[str, dict[str, object]]:
        """Actually call every provider, and report what each one said.

        Distinct from `health()`, which reports circuit-breaker state and so can
        only describe providers that have already been tried. This one is the
        difference between "nothing has gone wrong" and "everything works".

        Never raises, and deliberately does not touch the breakers: a probe is a
        diagnostic, and letting it trip a breaker would mean checking your
        providers could take one out of rotation.
        """
        results: dict[str, dict[str, object]] = {}
        for provider in self._providers:
            started = time.perf_counter()
            try:
                await asyncio.wait_for(
                    provider.generate("You are terse.", "Reply with: ok"),
                    timeout=timeout_seconds,
                )
                results[provider.name] = {
                    "ok": True,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "model": getattr(provider, "_model", None)
                    or getattr(provider, "model", ""),
                }
            except Exception as exc:  # noqa: BLE001 - the failure IS the result
                results[provider.name] = {
                    "ok": False,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "model": getattr(provider, "_model", None)
                    or getattr(provider, "model", ""),
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                }
        return results

    def health(self) -> dict[str, dict[str, object]]:
        """Per-provider breaker state, surfaced on the health endpoint so an
        operator can see *which* account is throttling rather than inferring it
        from a latency graph."""
        return {name: b.snapshot() for name, b in self._breakers.items()}

    async def generate(self, system: str, user: str) -> LLMResult:
        """Serve from the first healthy provider, in configured order.

        Two passes. The first honours the circuit breakers; if it served
        nothing *and* nothing actually failed — meaning every provider was
        breaker-skipped — a second pass ignores the breakers entirely. Stale
        in-process bookkeeping must be able to cost latency, never turn a
        recovered account into a hard outage.
        """
        last_exc: Exception | None = None
        for respect_breakers in (True, False):
            for i, provider in enumerate(self._providers):
                if self._skip(provider.name, respect_breakers):
                    continue
                try:
                    async with self._bulkheads[provider.name]():
                        # Re-checked here, after the bulkhead slot is acquired
                        # rather than before. Under a burst every request picks
                        # its provider at the same instant, so a check made
                        # before queuing is decided while the breaker is still
                        # closed — and the whole burst then piles onto the
                        # backend that the first few requests have already
                        # discovered is throttling. By the time a queued
                        # request reaches a free slot, that verdict is in.
                        if self._skip(provider.name, respect_breakers):
                            continue
                        result = await provider.generate(system, user)
                    self._record(provider.name, None)
                    return result
                except Exception as exc:  # noqa: BLE001 - degrade to the next backend
                    last_exc = exc
                    self._record(provider.name, exc)
                    nxt = (
                        self._providers[i + 1].name
                        if i + 1 < len(self._providers)
                        else None
                    )
                    log.warning(
                        "llm.failover",
                        provider=provider.name,
                        to=nxt,
                        rate_limited=is_rate_limited(exc),
                        error=f"{type(exc).__name__}: {exc}"[:200],
                    )
            if last_exc is not None:
                # A provider genuinely tried and failed, so the chain has been
                # exercised. Re-running it with the breakers ignored would
                # double every failing request's cost for no new information.
                break
        assert last_exc is not None  # non-empty chain guaranteed by __init__
        raise last_exc

    async def stream(
        self, system: str, user: str, on_provider: Callable[[str], None] | None = None
    ) -> AsyncIterator[str]:
        # Each leaf provider reports its own name via on_provider, so a fallback
        # overwrites the previous report and the caller ends with the backend
        # that actually served the stream.
        #
        # Failover only covers tokens not yet delivered: once this generator has
        # yielded anything, the caller has already seen partial output and
        # restarting on another backend would splice two different answers
        # together. So a mid-stream failure is raised rather than re-routed.
        # Same two-pass shape as generate(): honour the breakers first, and
        # fall back to ignoring them only if that skipped every provider
        # without anything actually failing.
        last_exc: Exception | None = None
        for respect_breakers in (True, False):
            for provider in self._providers:
                if self._skip(provider.name, respect_breakers):
                    continue
                produced = False
                try:
                    async with self._bulkheads[provider.name]():
                        if self._skip(provider.name, respect_breakers):
                            continue
                        async for tok in provider.stream(system, user, on_provider):
                            produced = True
                            yield tok
                    self._record(provider.name, None)
                    return
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    self._record(provider.name, exc)
                    log.warning(
                        "llm.failover_stream",
                        provider=provider.name,
                        mid_stream=produced,
                        rate_limited=is_rate_limited(exc),
                    )
                    if produced:
                        raise
            if last_exc is not None:
                break
        assert last_exc is not None  # non-empty chain guaranteed by __init__
        raise last_exc

    async def describe_image(self, data: bytes, content_type: str) -> str:
        # Same try-next-provider shape as generate() — a provider that isn't
        # multimodal (Groq, Ollama here) raises NotImplementedError, which is
        # just another reason to fall through to the next one in the chain.
        last_exc: Exception | None = None
        chain = self._providers
        for i, provider in enumerate(chain):
            try:
                async with self._bulkheads[provider.name]():
                    result = await provider.describe_image(data, content_type)
                self._record(provider.name, None)
                return result
            except NotImplementedError as exc:
                # Not a health signal — this backend simply isn't multimodal.
                # Tripping its breaker would take it out of rotation for text.
                last_exc = exc
                continue
            except Exception as exc:  # noqa: BLE001 - degrade to the next backend
                last_exc = exc
                self._record(provider.name, exc)
                nxt = chain[i + 1].name if i + 1 < len(chain) else None
                log.warning("llm.describe_image_failover", provider=provider.name, to=nxt)
        assert last_exc is not None  # non-empty chain guaranteed by __init__
        raise last_exc


_PROVIDERS = {
    "openai": OpenAIProvider,
    "groq": GroqProvider,
    "gemini": GeminiProvider,
    "ollama": OllamaProvider,
}

# Ollama is local (no key); every other backend needs its credential configured
# or it is silently dropped from the failover chain.
_PROVIDER_KEY = {
    "openai": lambda s: bool(s.openai_api_key),
    "groq": lambda s: bool(s.groq_api_key),
    "gemini": lambda s: bool(s.gemini_api_key),
    "ollama": lambda s: True,
}


def _build_provider(name: str, settings: Settings):  # type: ignore[no-untyped-def]
    try:
        return _PROVIDERS[name](settings)
    except KeyError:
        raise ValueError(f"Unknown generation provider: {name!r}") from None


def build_llm(settings: Settings) -> FailoverLLM:
    # Ordered, de-duplicated failover chain. A stage without configured
    # credentials is skipped so the chain starts at the first usable backend.
    ordered = [
        settings.generation_primary,
        settings.generation_secondary,
        settings.generation_tertiary,
    ]
    chain: list[str] = []
    for name in ordered:
        if name and name not in chain and _PROVIDER_KEY.get(name, lambda _s: True)(settings):
            chain.append(name)
    # If nothing is configured, keep the primary so the failure is explicit at
    # call time rather than hidden behind an empty chain.
    if not chain:
        chain = [settings.generation_primary]
    return FailoverLLM(
        [_build_provider(name, settings) for name in chain],
        breaker_threshold=settings.llm_breaker_threshold,
        breaker_cooldown_seconds=settings.llm_breaker_cooldown_seconds,
        max_concurrency_per_provider=settings.llm_max_concurrency_per_provider,
    )
