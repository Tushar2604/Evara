"""Answer a customer message as the AI receptionist.

The booking counterpart to `AskChatbot`. Both take a message on a session and
return an answer that gets persisted to the same conversation; the difference is
that this one runs the tool-using agent, so the reply can be the *result of
having booked something* rather than only words.

Shares AskChatbot's transaction discipline deliberately: no database connection
is held across the agent run, because that run makes several LLM calls and can
take tens of seconds. Holding a pooled connection for that would exhaust a
free-tier pool with a handful of concurrent conversations.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from src.application.agent.tools import ToolContext
from src.application.ports.repositories import UnitOfWork
from src.domain.chat.entities import Message, MessageRole
from src.domain.chat.events import MessageAnswered
from src.domain.safety.guardrails import format_message_history, scan_input
from src.domain.scheduling.slate import BookingSlate
from src.domain.shared.errors import NotFoundError, QuotaExceededError
from src.domain.shared.identifiers import ChatbotId, SessionId, TenantId

log = structlog.get_logger(__name__)

# How much of the thread the agent sees. Booking conversations are short, and
# the transcript is re-sent on every reasoning step — so this bounds cost
# quadratically, not linearly. Twenty turns is far more than any booking needs.
MAX_HISTORY_MESSAGES = 20


@dataclass(frozen=True)
class FrontOfficeAnswer:
    """What the receptionist said, and what it cost.

    Carries the stored message id so an HTTP caller can reference the reply the
    same way the retrieval path lets it — the agent persists its own answer, so
    without this the id would be unrecoverable.
    """

    answer: str
    message_id: uuid.UUID
    tokens_used: int
    provider: str


class AskFrontOffice:
    """Run the receptionist agent over one inbound message."""

    def __init__(self, uow: UnitOfWork, agent: object) -> None:
        self._uow = uow
        self._agent = agent

    async def execute(
        self,
        tenant_id: TenantId,
        session_id: SessionId,
        *,
        message: str,
        chatbot_id: ChatbotId | None = None,
        source: str = "api",
        channel: str = "",
        customer_phone: str = "",
        persist_user_message: bool = True,
    ) -> FrontOfficeAnswer:
        started = time.perf_counter()

        # --- 1. Validate, enforce quota, read history (short transaction) ---
        async with self._uow as uow:
            uow.set_tenant_scope(tenant_id)

            session = await uow.chats.get_session(tenant_id, session_id)
            if session is None:
                raise NotFoundError("Chat session not found.")
            answering_as = chatbot_id or session.chatbot_id
            if answering_as is None:
                raise NotFoundError("No assistant is attached to this conversation.")

            # The assistant's own Conversational Flow — Identity, Facts, its
            # own do's and don'ts. Without loading it here, the agent has
            # nothing but the loop's fixed tool-use rules to work from, on
            # every channel this use case serves.
            bot = await uow.chatbots.get(tenant_id, answering_as)
            if bot is None:
                raise NotFoundError("Assistant not found.")

            tenant = await uow.tenants.get(tenant_id)
            assert tenant is not None
            used = await uow.usage.tokens_used_today(tenant_id)
            if used >= tenant.daily_token_quota:
                raise QuotaExceededError("Daily token quota exceeded. Try again tomorrow.")

            # Only the tail ever gets used (below), so fetch just that instead
            # of the whole thread — see the identical note in ask_chatbot.py.
            prior = await uow.chats.list_messages(
                tenant_id, session_id, limit=MAX_HISTORY_MESSAGES
            )
            # "customer", not the default "candidate": this agent books
            # appointments, and a transcript that calls the other person a
            # candidate tells the model, every turn, that it is running a job
            # interview.
            history = format_message_history(
                prior[-MAX_HISTORY_MESSAGES:], user_label="customer"
            )

            # Where this booking has got to. The transcript above records what
            # was *said*; it cannot record the service id, the branch id, or the
            # instant behind "Thu 03 Sep, 9:00 AM". Without this the agent
            # re-derived those by asking the availability engine again on every
            # turn — and that call exists to produce a list to read out, so it
            # read the list out again instead of booking.
            saved_state = await uow.chats.get_booking_state(tenant_id, session_id)

            if persist_user_message:
                await uow.chats.add_message(
                    Message(
                        session_id=session_id,
                        tenant_id=tenant_id,
                        role=MessageRole.USER,
                        content=message,
                    )
                )
            await uow.commit()

        # The same injection screen the RAG path applies. An agent with booking
        # tools is a higher-value target than one that only reads documents, so
        # skipping it here would be the wrong way round.
        verdict = scan_input(message)
        if not verdict.allowed:
            answer = verdict.reason or "I can't help with that request."
            return await self._persist_answer(
                tenant_id, session_id, answering_as, answer, tokens=0, provider=""
            )

        # --- 2. Run the agent. No connection held. ---
        #
        # The slate goes in as a live object the scheduling tools write to, and
        # comes back out carrying whatever this turn learned — the times just
        # offered, the option the customer picked, the hold, the name. It is
        # rendered into the prompt as well, so the planner reads the same state
        # its tools are updating.
        slate = BookingSlate.from_dict(saved_state)
        # The channel's own identity counts as a detail already given. Without
        # this the slate reports "still needed: a phone number or email" on
        # WhatsApp, where the customer is literally messaging from the number —
        # and an assistant told something is missing goes and asks for it.
        slate.remember(phone=customer_phone)
        result = await self._agent.run(  # type: ignore[attr-defined]
            ToolContext(
                tenant_id=tenant_id,
                chatbot_id=answering_as,
                # What the tools need to attribute the booking and to know who
                # they are talking to without asking.
                extras={
                    "source": source,
                    "channel": channel,
                    "customer_phone": customer_phone,
                    # Scopes booking idempotency to this conversation.
                    "conversation_id": str(session_id),
                    "slate": slate,
                },
            ),
            message,
            history=history,
            tenant_prompt=bot.system_prompt,
            response_language=bot.assistant.response_language,
            state_block=slate.render(datetime.now(UTC)),
        )

        # --- 3. Persist the reply and meter it (short transaction) ---
        updated_state = slate.to_dict()
        stored = await self._persist_answer(
            tenant_id,
            session_id,
            answering_as,
            result.answer,
            tokens=result.trace.tokens_used,
            provider=result.trace.provider or "",
            # Only written when the run actually moved the booking on, so an
            # ordinary question on a booking-capable assistant costs no write.
            booking_state=updated_state if updated_state != (saved_state or {}) else None,
        )

        log.info(
            "front_office.answered",
            tenant_id=str(tenant_id),
            source=source,
            steps=result.trace.num_steps,
            tools=",".join(result.trace.tools_used()),
            stop_reason=result.trace.stop_reason,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        return stored

    async def _persist_answer(
        self,
        tenant_id: TenantId,
        session_id: SessionId,
        chatbot_id: ChatbotId,
        answer: str,
        *,
        tokens: int,
        provider: str,
        booking_state: dict | None = None,
    ) -> FrontOfficeAnswer:
        """Store the reply, meter it, and — if this turn moved a booking on —
        save the slate in the same transaction.

        `booking_state` is `None` for "unchanged, do not write" and `{}` for
        "this conversation has no booking in flight any more". Two meanings that
        would collapse into one if the absence of a write were used for both.
        """
        async with self._uow as uow:
            uow.set_tenant_scope(tenant_id)
            if booking_state is not None:
                await uow.chats.save_booking_state(tenant_id, session_id, booking_state)
            stored = Message(
                session_id=session_id,
                tenant_id=tenant_id,
                role=MessageRole.ASSISTANT,
                content=answer,
                tokens_used=tokens,
                provider=provider or None,
            )
            await uow.chats.add_message(stored)
            if tokens:
                await uow.usage.add_tokens(tenant_id, tokens)
            uow.collect_event(
                MessageAnswered(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    chatbot_id=chatbot_id,
                    tokens_used=tokens,
                    provider=provider,
                )
            )
            await uow.commit()
        return FrontOfficeAnswer(
            answer=answer, message_id=stored.id, tokens_used=tokens, provider=provider
        )
