"""Answer a user message over a chatbot's documents (the RAG flow).

Orchestration steps (mirrored by the LangGraph graph in infrastructure):
  retrieve -> assemble context + citations -> enforce token quota -> generate
  -> persist message + record usage -> emit MessageAnswered.

The actual LLM call goes through a failover router (Groq primary, Gemini
fallback) supplied as the `llm` port, so free-tier rate limits degrade
gracefully instead of erroring.
"""

from __future__ import annotations

import time

import structlog

from src.application.dtos import AnswerOutput, AskInput, CitationOut
from src.application.ports.repositories import RequestLog, UnitOfWork
from src.application.ports.services import Embedder, LLMProvider, LLMResult
from src.config.settings import get_settings
from src.domain.chat.entities import Citation, Message, MessageRole
from src.domain.chat.events import MessageAnswered
from src.domain.chat.relevance import prune_citations
from src.domain.chatbot.entities import Chatbot
from src.domain.safety.guardrails import (
    GUARD_REFUSAL,
    NO_CONTEXT_MARKER,
    build_grounded_prompt,
    count_repeat_asks,
    format_message_history,
    scan_input,
    scan_output,
)
from src.domain.shared.errors import NotFoundError, QuotaExceededError
from src.domain.shared.identifiers import ChatbotId, SessionId, TenantId

log = structlog.get_logger(__name__)

# The canonical redirect opener from DEFAULT_SYSTEM_PROMPT; heuristic for custom
# prompts, but no_context (citation-based) is the prompt-independent signal.
_REFUSAL_PREFIX = "I'm here to help with our open roles and your application"


def _build_context(citations: list[Citation]) -> str:
    blocks = [
        f"[Source {c.ordinal} | doc={c.document_id} | score={c.score:.3f}]\n{c.snippet}"
        for c in citations
    ]
    return "\n\n".join(blocks) if blocks else NO_CONTEXT_MARKER


def _retrieved_payload(citations: list[Citation]) -> list[dict]:
    return [
        {
            "chunk_id": c.chunk_id,
            "document_id": str(c.document_id),
            "ordinal": c.ordinal,
            "score": c.score,
        }
        for c in citations
    ]


def _max_score(citations: list[Citation]) -> float | None:
    return max((c.score for c in citations), default=None)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


class AskChatbot:
    def __init__(self, uow: UnitOfWork, embedder: Embedder, llm: LLMProvider) -> None:
        self._uow = uow
        self._embedder = embedder
        self._llm = llm

    async def execute(
        self, tenant_id: TenantId, session_id: SessionId, data: AskInput
    ) -> AnswerOutput:
        started = time.perf_counter()
        # IMPORTANT: no DB connection is held across the embedding or LLM calls.
        # Free-tier Postgres (Neon) caps connections aggressively, so each
        # transaction below is short-lived and released before any slow network
        # call (embed / generate) runs.

        # --- 1. Validate, enforce quota, persist the user message (short txn) ---
        async with self._uow as uow:
            uow.set_tenant_scope(tenant_id)

            session = await uow.chats.get_session(tenant_id, session_id)
            if session is None:
                raise NotFoundError("Chat session not found.")
            # The caller's choice wins over the session's own, and a session
            # with no assistant at all is only answerable when one is supplied.
            answering_as = ChatbotId(data.chatbot_id) if data.chatbot_id else session.chatbot_id
            if answering_as is None:
                raise NotFoundError("No assistant is attached to this conversation.")
            chatbot = await uow.chatbots.get(tenant_id, answering_as)
            if chatbot is None:
                raise NotFoundError("Chatbot not found.")

            tenant = await uow.tenants.get(tenant_id)
            assert tenant is not None
            used = await uow.usage.tokens_used_today(tenant_id)
            if used >= tenant.daily_token_quota:
                raise QuotaExceededError("Daily token quota exceeded. Try again tomorrow.")

            # Fetched BEFORE the current message is added, so it reflects prior
            # turns only — the current message is passed separately as `data.message`.
            # Limited to the tail: both `format_message_history` and
            # `count_repeat_asks` only ever look at the last `_HISTORY_TURNS`
            # (12) messages, so a long-lived WhatsApp thread was paying to
            # fetch and format its entire history on every single turn for no
            # benefit — growing latency and DB-connection hold time as the
            # conversation aged. 20 leaves headroom over that constant.
            prior = await uow.chats.list_messages(tenant_id, session_id, limit=20)
            history_text = format_message_history(prior)
            # Counted here, on the turns that came BEFORE this message, so a
            # candidate circling back to the same unanswered question gets a
            # different answer rather than the same sentence again.
            repeat_count = count_repeat_asks(prior, data.message)

            if data.persist_user_message:
                await uow.chats.add_message(
                    Message(
                        session_id=session_id,
                        tenant_id=tenant_id,
                        role=MessageRole.USER,
                        content=data.message,
                    )
                )
            await uow.commit()

        # --- 2. Retrieve (embedding + vector search, own short txn) ---
        citations = await self._retrieve(tenant_id, chatbot, data.message)

        # --- 3. Generate (NO transaction open — the slow call runs pool-free) ---
        # Guardrails wrap the generation: screen the user message for injection,
        # isolate untrusted text in labelled blocks, and screen the answer for
        # system-prompt leakage. A high-risk verdict short-circuits to a refusal.
        context = _build_context(citations)
        user_prompt = build_grounded_prompt(
            context,
            data.message,
            history=history_text,
            repeat_count=repeat_count,
            response_language=chatbot.assistant.response_language,
        )

        input_verdict = scan_input(data.message)
        if not input_verdict.allowed:
            log.warning(
                "guardrail.input_blocked",
                tenant_id=str(tenant_id),
                categories=input_verdict.categories,
            )
            result = LLMResult(
                text=GUARD_REFUSAL, tokens_used=0, provider="guardrail", model="guardrail"
            )
        else:
            try:
                result = await self._llm.generate(chatbot.system_prompt, user_prompt)
            except Exception as exc:  # noqa: BLE001 - record the failed request, then surface it
                await self._log_request(
                    tenant_id,
                    chatbot,
                    session_id,
                    data.message,
                    citations,
                    started,
                    status="error",
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise

            output_verdict = scan_output(result.text, system_prompt=chatbot.system_prompt)
            if not output_verdict.allowed:
                log.warning(
                    "guardrail.output_blocked",
                    tenant_id=str(tenant_id),
                    categories=output_verdict.categories,
                )
                result = LLMResult(
                    text=GUARD_REFUSAL,
                    tokens_used=result.tokens_used,
                    provider=result.provider,
                    model=result.model,
                )

        refused = result.text.strip().startswith(_REFUSAL_PREFIX)

        # --- 4. Persist answer + usage + event + request log (short txn) ---
        answer = Message(
            session_id=session_id,
            tenant_id=tenant_id,
            role=MessageRole.ASSISTANT,
            content=result.text,
            citations=citations,
            tokens_used=result.tokens_used,
            provider=result.provider,
        )
        async with self._uow as uow:
            uow.set_tenant_scope(tenant_id)
            await uow.chats.add_message(answer)
            await uow.usage.add_tokens(tenant_id, result.tokens_used)
            await uow.request_logs.add(
                RequestLog(
                    tenant_id=tenant_id,
                    chatbot_id=chatbot.id,
                    session_id=session_id,
                    message_id=answer.id,
                    query=data.message,
                    retrieved=_retrieved_payload(citations),
                    num_retrieved=len(citations),
                    max_score=_max_score(citations),
                    no_context=not citations,
                    refused=refused,
                    answer=result.text,
                    provider=result.provider,
                    model=result.model,
                    tokens_used=result.tokens_used,
                    status="ok",
                    latency_ms=_elapsed_ms(started),
                )
            )
            uow.collect_event(
                MessageAnswered(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    chatbot_id=chatbot.id,
                    tokens_used=result.tokens_used,
                    provider=result.provider,
                )
            )
            await uow.commit()

        return AnswerOutput(
            message_id=answer.id,
            answer=result.text,
            citations=[
                CitationOut(
                    document_id=c.document_id,
                    chunk_id=c.chunk_id,
                    ordinal=c.ordinal,
                    score=c.score,
                    snippet=c.snippet,
                )
                for c in citations
            ],
            tokens_used=result.tokens_used,
            provider=result.provider,
        )

    async def _log_request(
        self,
        tenant_id: TenantId,
        chatbot: Chatbot,
        session_id: SessionId,
        query: str,
        citations: list[Citation],
        started: float,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        """Write a request log in its own short txn. Never raises — a logging
        failure must not mask the original error or break the response."""
        try:
            async with self._uow as uow:
                uow.set_tenant_scope(tenant_id)
                await uow.request_logs.add(
                    RequestLog(
                        tenant_id=tenant_id,
                        chatbot_id=chatbot.id,
                        session_id=session_id,
                        query=query,
                        retrieved=_retrieved_payload(citations),
                        num_retrieved=len(citations),
                        max_score=_max_score(citations),
                        no_context=not citations,
                        status=status,
                        error=error,
                        latency_ms=_elapsed_ms(started),
                    )
                )
                await uow.commit()
        except Exception:  # noqa: BLE001 - logging is best-effort
            log.exception("request_log.write_failed")

    async def _retrieve(
        self, tenant_id: TenantId, chatbot: Chatbot, query: str
    ) -> list[Citation]:
        # Embed first (network call, no DB connection held), then open a short
        # read transaction only for the vector search.
        query_vec = await self._embedder.embed_query(query)
        # An assistant's own floor wins when it set one; otherwise the platform
        # floor applies. Without it `min_score=0.0` hands the model the k
        # least-irrelevant chunks in the knowledge base for *any* question, and
        # the prompt then presents them as the source of truth — which is how a
        # bot ends up answering from the wrong document with total confidence.
        min_score = max(chatbot.retrieval.min_score, get_settings().retrieval_min_score_floor)
        async with self._uow as uow:
            uow.set_tenant_scope(tenant_id)
            hits = await uow.chunks.search(
                tenant_id=chatbot.tenant_id,
                query_embedding=query_vec,
                top_k=chatbot.retrieval.top_k,
                document_ids=chatbot.document_filter(),
                min_score=min_score,
            )
        citations = prune_citations(
            [
                Citation(
                    document_id=chunk.document_id,
                    chunk_id=chunk.id,
                    ordinal=chunk.ordinal,
                    score=score,
                    snippet=chunk.text[:500],
                )
                for chunk, score in hits
            ],
            min_score=min_score,
        )
        # The one line that answers "why did it say that?" — how many documents
        # the assistant may read from, how much came back, and how well it
        # matched. A `kb_documents=0` here is the whole explanation for an
        # assistant that ignores a knowledge base someone believes they attached.
        log.info(
            "rag.retrieved",
            tenant_id=str(tenant_id),
            chatbot_id=str(chatbot.id),
            kb_documents=len(chatbot.document_filter()),
            hits=len(hits),
            kept=len(citations),
            min_score=min_score,
            top_score=round(max((s for _, s in hits), default=0.0), 3),
        )
        return citations
