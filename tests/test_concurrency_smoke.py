"""End-to-end concurrency checks for the pieces that carry simultaneous traffic.

These exercise the seams that decide whether many chatbots can be served at
once — the shared HTTP pool and the per-provider pacing — rather than any one
provider's SDK. They are deliberately hermetic: no network, no database, so
they run in CI and actually fail when a regression lands.
"""

from __future__ import annotations

import asyncio

import anyio
import httpx
import pytest
from src.infrastructure.http_client import close_clients, get_client
from src.infrastructure.llm.embeddings import GeminiEmbedder
from src.infrastructure.persistence.unit_of_work import shielded


class _Settings:
    """Only the fields GeminiEmbedder reads."""

    embedding_model = "gemini-embedding-001"
    embedding_dim = 8
    gemini_api_key = "test-key"
    embedding_max_concurrency = 4


@pytest.mark.asyncio
async def test_the_http_pool_is_shared_rather_than_rebuilt_per_call() -> None:
    """The regression this guards: every integration used to open its own
    `AsyncClient` per request, paying a TLS handshake each time and leaking
    sockets into TIME_WAIT under load."""
    try:
        first = await get_client("smoke-pool")
        again = await get_client("smoke-pool")
        assert first is again, "the same named pool must be reused, not rebuilt"

        other = await get_client("smoke-other")
        assert other is not first, (
            "different names must get separate pools, so a slow dependency "
            "cannot consume the connection budget of a fast one"
        )
    finally:
        await close_clients()


@pytest.mark.asyncio
async def test_a_closed_pool_is_rebuilt_on_next_use() -> None:
    """Shutdown closes the pools; a task still running afterwards must get a
    working client rather than an `is_closed` one."""
    try:
        first = await get_client("smoke-reopen")
        await close_clients()
        second = await get_client("smoke-reopen")
        assert not second.is_closed
        assert second is not first
    finally:
        await close_clients()


@pytest.mark.asyncio
async def test_concurrent_pool_creation_yields_exactly_one_client() -> None:
    """Racing coroutines on a cold cache must not each build a pool — the
    losers' pools would be dropped on the floor with their sockets open."""
    try:
        clients = await asyncio.gather(
            *(get_client("smoke-race") for _ in range(30))
        )
        assert len({id(c) for c in clients}) == 1
    finally:
        await close_clients()


@pytest.mark.asyncio
async def test_embedding_bursts_are_paced_not_dropped() -> None:
    """A batch of embeddings must all complete, with in-flight calls capped.

    Embeddings run on every retrieval, so an unbounded batch is how a document
    ingestion starves the live chatbots of the same free-tier quota.
    """
    inflight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        try:
            await asyncio.sleep(0)
            return httpx.Response(200, json={"embedding": {"values": [0.1] * 8}})
        finally:
            inflight -= 1

    embedder = GeminiEmbedder(_Settings())
    transport = httpx.MockTransport(handler)
    stub = httpx.AsyncClient(transport=transport)

    import src.infrastructure.llm.embeddings as embeddings_module

    original = embeddings_module.get_client
    embeddings_module.get_client = lambda *a, **k: _ready(stub)  # type: ignore[assignment]
    try:
        vectors = await embedder.embed_documents([f"chunk {i}" for i in range(40)])
    finally:
        embeddings_module.get_client = original  # type: ignore[assignment]
        await stub.aclose()

    assert len(vectors) == 40, "every chunk must be embedded, not dropped"
    assert all(len(v) == 8 for v in vectors)
    assert peak <= _Settings.embedding_max_concurrency, (
        f"bulkhead breached: {peak} embedding calls were in flight at once"
    )


async def _ready(client: httpx.AsyncClient) -> httpx.AsyncClient:
    return client


@pytest.mark.asyncio
async def test_embedding_order_survives_concurrency() -> None:
    """Gathering must not scramble results — callers zip these back onto their
    chunks positionally, so a reordering would silently mis-file every vector
    against the wrong text."""
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        import json

        text = json.loads(request.content)["content"]["parts"][0]["text"]
        seen.append(text)
        # Reverse the natural completion order: later chunks answer first.
        await asyncio.sleep(0.01 if "0" in text else 0)
        return httpx.Response(
            200, json={"embedding": {"values": [float(len(text))] * 8}}
        )

    embedder = GeminiEmbedder(_Settings())
    stub = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    import src.infrastructure.llm.embeddings as embeddings_module

    original = embeddings_module.get_client
    embeddings_module.get_client = lambda *a, **k: _ready(stub)  # type: ignore[assignment]
    try:
        texts = ["chunk 0", "chunk 11", "chunk 222"]
        vectors = await embedder.embed_documents(texts)
    finally:
        embeddings_module.get_client = original  # type: ignore[assignment]
        await stub.aclose()

    assert [v[0] for v in vectors] == [float(len(t)) for t in texts]


@pytest.mark.asyncio
async def test_shielded_block_finishes_despite_outer_cancellation() -> None:
    """The regression this guards: an SSE client disconnecting mid-stream
    cancels the generator's task via an anyio cancel scope. If that lands
    inside an unshielded `async with unit_of_work()`, the session's own
    close is cancelled too and the pooled asyncpg connection is abandoned
    for the garbage collector to find (see routers/chat.py's `_log` and
    post-stream persist blocks). `shielded()` must let a "durable write"
    block run to completion even when its enclosing task is cancelled."""
    finished = False

    async def durable_write() -> None:
        nonlocal finished
        async with shielded():
            await asyncio.sleep(0.05)
            finished = True

    async with anyio.create_task_group() as tg:
        tg.start_soon(durable_write)
        await asyncio.sleep(0)  # let durable_write start and enter the shield
        tg.cancel_scope.cancel()

    assert finished, "a shielded write must complete even if its caller is cancelled"


@pytest.mark.asyncio
async def test_unshielded_block_is_the_regression_shielded_prevents() -> None:
    """Sanity check for the test above: without the shield, the same
    cancellation cuts the block off before it finishes — confirming the
    fixture actually exercises cancellation, not just a no-op scope."""
    finished = False

    async def unprotected_write() -> None:
        nonlocal finished
        await asyncio.sleep(0.05)
        finished = True

    async with anyio.create_task_group() as tg:
        tg.start_soon(unprotected_write)
        await asyncio.sleep(0)  # let unprotected_write start, matching the test above
        tg.cancel_scope.cancel()

    assert not finished, "expected the unshielded write to be cut off by cancellation"
