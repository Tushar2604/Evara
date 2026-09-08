"""Public widget API — unauthenticated chat for embedded / shared chatbots.

This is the surface a third-party website talks to. There is no JWT and no API
key here; a chatbot is identified solely by its publishable key. Defense in depth
for anonymous traffic, in order of evaluation:

  1. Key resolves to a chatbot AND that chatbot is public      (else 404)
  2. The request `Origin` is on the chatbot's allowlist         (else 403)
  3. Per-IP+bot sliding-window rate limit                       (else 429)
  4. The owning tenant's daily token quota                      (else 429)

CORS headers for these routes are added by PublicCorsMiddleware; the allowlist
check above is the *authorization* layer (CORS only governs browser exposure).
"""

from __future__ import annotations

import json
import time
import uuid

import structlog
from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from src.application.dtos import AskInput
from src.application.ports.repositories import RequestLog
from src.application.use_cases.ask_chatbot import AskChatbot
from src.application.use_cases.front_office import AskFrontOffice
from src.domain.chat.entities import ChatSession, Message, MessageRole
from src.domain.chatbot.entities import (
    Chatbot,
    opener_instruction,
    origin_allowed,
    static_welcome,
)
from src.domain.safety.guardrails import build_grounded_prompt, language_rules
from src.domain.shared.identifiers import SessionId
from src.infrastructure.persistence.unit_of_work import shielded
from src.infrastructure.rag.graph import RagGraph, build_context
from src.interfaces.api.deps import ContainerDep
from src.interfaces.api.routers.chat import _as_chunks
from src.interfaces.api.schemas import (
    CreateSessionResponse,
    PublicAnswerResponse,
    PublicCitation,
    PublicConfigResponse,
    WidgetConfigSchema,
)

_log_public = structlog.get_logger(__name__)

router = APIRouter(prefix="/public", tags=["public"])

# Path prefix the CORS middleware keys off. Kept here next to the router so the
# two stay in sync.
PUBLIC_PATH_PREFIX = "/api/v1/public"


def client_ip(request: Request) -> str:
    """Best-effort client IP. Honours the first hop of X-Forwarded-For (set by
    the platform's proxy) and falls back to the socket peer."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def request_origin(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin:
        return origin
    # Some embeds (and the hosted page on same-origin nav) omit Origin; fall back
    # to the Referer's origin so the allowlist can still be evaluated.
    referer = request.headers.get("referer")
    if referer:
        parts = referer.split("/")
        if len(parts) >= 3:
            return "/".join(parts[:3])
    return None


async def _resolve_public(public_key: str, request: Request, container: ContainerDep) -> Chatbot:
    """Resolve + authorize a public chatbot for this request (steps 1–2)."""
    async with container.unit_of_work() as uow:
        bot = await uow.chatbots.get_by_public_key(public_key)
    if bot is None:
        raise HTTPException(status_code=404, detail="Chatbot not found")
    if not origin_allowed(request_origin(request), bot.allowed_origins):
        raise HTTPException(
            status_code=403, detail="This site is not allowed to embed this chatbot."
        )
    return bot


def _enforce_rate_limit(container: ContainerDep, bot: Chatbot, request: Request) -> None:
    key = f"{bot.public_key}:{client_ip(request)}"
    if not container.anon_rate_limiter.allow(key):
        raise HTTPException(
            status_code=429,
            detail="Too many messages. Please slow down and try again shortly.",
        )


def _to_widget_schema(bot: Chatbot) -> WidgetConfigSchema:
    return WidgetConfigSchema(
        theme_color=bot.widget.theme_color,
        display_name=bot.widget.display_name,
        welcome_message=bot.widget.welcome_message,
        launcher_position=bot.widget.launcher_position,  # type: ignore[arg-type]
    )


@router.get("/chatbots/{public_key}/config", response_model=PublicConfigResponse)
async def widget_config(
    public_key: str, request: Request, container: ContainerDep
) -> PublicConfigResponse:
    bot = await _resolve_public(public_key, request, container)
    return PublicConfigResponse(
        chatbot_id=bot.id,
        name=bot.name,
        channel=bot.channel,
        widget=_to_widget_schema(bot),
        languages=list(bot.assistant.languages),
    )


@router.post(
    "/chatbots/{public_key}/sessions",
    response_model=CreateSessionResponse,
    status_code=201,
)
async def create_public_session(
    public_key: str, request: Request, container: ContainerDep
) -> CreateSessionResponse:
    bot = await _resolve_public(public_key, request, container)
    session = ChatSession(tenant_id=bot.tenant_id, chatbot_id=bot.id)
    async with container.unit_of_work() as uow:
        uow.set_tenant_scope(bot.tenant_id)
        await uow.chats.add_session(session)
        await uow.commit()
    return CreateSessionResponse(session_id=session.id)


async def _load_session_bot(
    public_key: str, session_id: uuid.UUID, request: Request, container: ContainerDep
) -> tuple[Chatbot, SessionId]:
    bot = await _resolve_public(public_key, request, container)
    sid = SessionId(session_id)
    async with container.unit_of_work() as uow:
        uow.set_tenant_scope(bot.tenant_id)
        session = await uow.chats.get_session(bot.tenant_id, sid)
    if session is None or session.chatbot_id != bot.id:
        raise HTTPException(status_code=404, detail="Session not found")
    return bot, sid


@router.post(
    "/chatbots/{public_key}/sessions/{session_id}/messages",
    response_model=PublicAnswerResponse,
)
async def ask_public(
    public_key: str,
    session_id: uuid.UUID,
    body: dict,
    request: Request,
    container: ContainerDep,
) -> PublicAnswerResponse:
    bot, sid = await _load_session_bot(public_key, session_id, request, container)
    _enforce_rate_limit(container, bot, request)
    message = str(body.get("message", "")).strip()
    if not message:
        raise HTTPException(status_code=422, detail="message is required")

    use_case = AskChatbot(container.unit_of_work(), container.embedder, container.llm)
    result = await use_case.execute(bot.tenant_id, sid, AskInput(message=message))
    return PublicAnswerResponse(
        answer=result.answer,
        citations=[
            PublicCitation(ordinal=c.ordinal, score=c.score, snippet=c.snippet)
            for c in result.citations
        ],
    )


def _public_agent_stream(container, bot, session_id, message: str):  # type: ignore[no-untyped-def]
    """The booking agent over the public SSE contract.

    Same shape as the authenticated path's `_agent_stream`, with one
    difference: the visitor's message is already stored by the caller (it is
    written alongside the quota check), so the use case is told not to store it
    again.
    """

    async def generator():  # type: ignore[no-untyped-def]
        # Always empty, and always sent: the widget waits for this event before
        # it renders anything, and an agent answer is grounded in tool results
        # rather than in retrieved documents.
        yield {"event": "citations", "data": "[]"}
        try:
            reply = await AskFrontOffice(
                container.unit_of_work(), container.front_office_agent
            ).execute(
                bot.tenant_id,
                session_id,
                message=message,
                chatbot_id=bot.id,
                source="public_widget",
                channel="web",
                persist_user_message=False,
            )
        except Exception:  # noqa: BLE001 - the stream still has to end cleanly
            _log_public.exception("front_office.public_stream_failed")
            yield {"event": "error", "data": json.dumps({"detail": "Generation failed."})}
            return

        for chunk in _as_chunks(reply.answer):
            yield {"event": "token", "data": chunk}
        yield {
            "event": "done",
            "data": json.dumps({"tokens_used": reply.tokens_used}),
        }

    return EventSourceResponse(generator())


@router.post("/chatbots/{public_key}/sessions/{session_id}/stream")
async def ask_public_stream(
    public_key: str,
    session_id: uuid.UUID,
    body: dict,
    request: Request,
    container: ContainerDep,
) -> EventSourceResponse:
    """SSE token stream for the embedded widget. Mirrors the authenticated chat
    stream but scopes everything to the chatbot resolved from the public key and
    never exposes internal document ids to the page."""
    bot, sid = await _load_session_bot(public_key, session_id, request, container)
    _enforce_rate_limit(container, bot, request)
    message = str(body.get("message", "")).strip()
    if not message:
        raise HTTPException(status_code=422, detail="message is required")

    started = time.perf_counter()

    # Persist the user turn + enforce the tenant quota in a short txn.
    async with container.unit_of_work() as uow:
        uow.set_tenant_scope(bot.tenant_id)
        tenant = await uow.tenants.get(bot.tenant_id)
        used = await uow.usage.tokens_used_today(bot.tenant_id)
        if tenant and used >= tenant.daily_token_quota:
            raise HTTPException(
                status_code=429, detail="This assistant has reached its daily limit."
            )
        await uow.chats.add_message(
            Message(
                session_id=sid,
                tenant_id=bot.tenant_id,
                role=MessageRole.USER,
                content=message,
            )
        )
        await uow.commit()

    # An assistant with appointments enabled answers through the booking agent
    # on the public surfaces too. Without this, a share link or an embedded
    # widget ran plain retrieval with no tools: it could not check availability
    # or write a booking, was never told so, and answered as though it had.
    if bot.assistant.appointments_enabled:
        return _public_agent_stream(container, bot, sid, message)

    graph = RagGraph(_ChunkRepoProxy(container), container.embedder, container.llm)
    citations = await graph.retrieve_only(bot, message)
    context = build_context(citations)

    retrieved_payload = [
        {
            "chunk_id": c.chunk_id,
            "document_id": str(c.document_id),
            "ordinal": c.ordinal,
            "score": c.score,
        }
        for c in citations
    ]
    max_score = max((c.score for c in citations), default=None)

    async def _log(**fields) -> None:  # type: ignore[no-untyped-def]
        try:
            async with shielded(), container.unit_of_work() as uow:
                uow.set_tenant_scope(bot.tenant_id)
                await uow.request_logs.add(
                    RequestLog(
                        tenant_id=bot.tenant_id,
                        chatbot_id=bot.id,
                        session_id=sid,
                        query=message,
                        retrieved=retrieved_payload,
                        num_retrieved=len(citations),
                        max_score=max_score,
                        no_context=not citations,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        **fields,
                    )
                )
                await uow.commit()
        except Exception:  # noqa: BLE001 - logging is best-effort
            pass

    async def event_generator():  # type: ignore[no-untyped-def]
        # Public citations omit internal document ids — only ordinal/score/snippet.
        yield {
            "event": "citations",
            "data": json.dumps(
                [
                    {"ordinal": c.ordinal, "score": c.score, "snippet": c.snippet}
                    for c in citations
                ]
            ),
        }
        # Isolate untrusted reference material + candidate message in labelled
        # blocks (injection defence); mirrors the authenticated stream path.
        prompt = build_grounded_prompt(
            context, message, response_language=bot.assistant.response_language
        )
        full: list[str] = []
        served_by: dict[str, str] = {}
        try:
            async for token in container.llm.stream(
                bot.system_prompt,
                prompt,
                on_provider=lambda name: served_by.__setitem__("provider", name),
            ):
                full.append(token)
                yield {"event": "token", "data": token}
        except Exception as exc:  # noqa: BLE001 - log then end the stream
            await _log(
                status="error",
                error=f"{type(exc).__name__}: {exc}",
                provider=served_by.get("provider"),
            )
            yield {"event": "error", "data": json.dumps({"detail": "Generation failed."})}
            return

        answer_text = "".join(full)
        tokens_used = max(1, (len(bot.system_prompt) + len(prompt) + len(answer_text)) // 4)
        refused = answer_text.strip().startswith(
            "I'm here to help with our open roles and your application"
        )

        assistant = Message(
            session_id=sid,
            tenant_id=bot.tenant_id,
            role=MessageRole.ASSISTANT,
            content=answer_text,
            citations=citations,
            tokens_used=tokens_used,
            provider=served_by.get("provider"),
        )
        # Shielded — see the identical note in routers/chat.py's stream handler.
        async with shielded(), container.unit_of_work() as uow:
            uow.set_tenant_scope(bot.tenant_id)
            await uow.chats.add_message(assistant)
            await uow.usage.add_tokens(bot.tenant_id, tokens_used)
            await uow.commit()

        await _log(
            status="ok",
            message_id=assistant.id,
            answer=answer_text,
            refused=refused,
            provider=served_by.get("provider"),
            tokens_used=tokens_used,
        )
        yield {"event": "done", "data": json.dumps({"tokens_used": tokens_used})}

    return EventSourceResponse(event_generator())


@router.post("/chatbots/{public_key}/sessions/{session_id}/greeting")
async def greet_public(
    public_key: str,
    session_id: uuid.UUID,
    request: Request,
    container: ContainerDep,
) -> EventSourceResponse:
    """AI-generated opening turn for the embedded widget / share page — same SSE
    contract as /stream but with no user input. Only valid on a brand-new session
    (no prior messages)."""
    bot, sid = await _load_session_bot(public_key, session_id, request, container)
    _enforce_rate_limit(container, bot, request)

    started = time.perf_counter()

    async with container.unit_of_work() as uow:
        uow.set_tenant_scope(bot.tenant_id)
        existing = await uow.chats.list_messages(bot.tenant_id, sid)
        if existing:
            raise HTTPException(status_code=400, detail="Session already started")
        tenant = await uow.tenants.get(bot.tenant_id)
        used = await uow.usage.tokens_used_today(bot.tenant_id)
        if tenant and used >= tenant.daily_token_quota:
            raise HTTPException(
                status_code=429, detail="This assistant has reached its daily limit."
            )

    # With Dynamic off the operator wants their exact words — no model call.
    verbatim = static_welcome(bot.assistant)
    instruction = opener_instruction(bot.assistant)

    async def event_generator():  # type: ignore[no-untyped-def]
        yield {"event": "citations", "data": "[]"}
        full: list[str] = []
        served_by: dict[str, str] = {}
        if verbatim is not None:
            full.append(verbatim)
            yield {"event": "token", "data": verbatim}
        else:
            try:
                async for token in container.llm.stream(
                    f"{language_rules(bot.assistant.response_language)}\n\n{bot.system_prompt}",
                    instruction,
                    on_provider=lambda name: served_by.__setitem__("provider", name),
                ):
                    full.append(token)
                    yield {"event": "token", "data": token}
            except Exception:  # noqa: BLE001 - end the stream; frontend falls back
                yield {"event": "error", "data": json.dumps({"detail": "Generation failed."})}
                return

        answer_text = "".join(full)
        tokens_used = (
            0
            if verbatim is not None
            else max(1, (len(bot.system_prompt) + len(instruction) + len(answer_text)) // 4)
        )

        assistant = Message(
            session_id=sid,
            tenant_id=bot.tenant_id,
            role=MessageRole.ASSISTANT,
            content=answer_text,
            tokens_used=tokens_used,
            provider=served_by.get("provider"),
        )
        async with shielded(), container.unit_of_work() as uow:  # see chat_stream's note
            uow.set_tenant_scope(bot.tenant_id)
            await uow.chats.add_message(assistant)
            await uow.usage.add_tokens(bot.tenant_id, tokens_used)
            try:
                await uow.request_logs.add(
                    RequestLog(
                        tenant_id=bot.tenant_id,
                        chatbot_id=bot.id,
                        session_id=sid,
                        message_id=assistant.id,
                        query="(session start)",
                        answer=answer_text,
                        status="ok",
                        provider=served_by.get("provider"),
                        tokens_used=tokens_used,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                    )
                )
            except Exception:  # noqa: BLE001 - logging is best-effort
                pass
            await uow.commit()

        yield {"event": "done", "data": json.dumps({"tokens_used": tokens_used})}

    return EventSourceResponse(event_generator())


class _ChunkRepoProxy:
    """Runs a read-only vector search in its own short transaction (so no DB
    connection is held across the SSE stream). Mirrors the one in chat.py."""

    def __init__(self, container: ContainerDep) -> None:
        self._container = container

    async def search(self, **kwargs):  # type: ignore[no-untyped-def]
        async with self._container.unit_of_work() as uow:
            uow.set_tenant_scope(kwargs["tenant_id"])
            return await uow.chunks.search(**kwargs)
