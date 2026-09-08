"""Virtual Interview — admin scheduling (authenticated) + the candidate's
conduct flow (token-scoped, no account needed — mirrors how public.py scopes
by a chatbot's publishable key instead of a JWT/API key).
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime

import anyio
from fastapi import APIRouter, HTTPException, Request, Response
from sse_starlette.sse import EventSourceResponse

from src.application.use_cases.finalize_interview import FinalizeInterview
from src.application.use_cases.schedule_interview import ScheduleInterview
from src.config.settings import get_settings
from src.domain.interview.entities import INTERVIEWER_SYSTEM_PROMPT, Interview, TranscriptTurn
from src.domain.interview.prompts import build_interview_turn_prompt, format_transcript
from src.domain.safety.guardrails import GUARD_REFUSAL, scan_input
from src.domain.shared.identifiers import DocumentId, InterviewId
from src.infrastructure.persistence.unit_of_work import shielded
from src.interfaces.api.deps import AdminPrincipalDep, ContainerDep
from src.interfaces.api.schemas import (
    AskRequest,
    InterviewBootstrapResponse,
    InterviewResponse,
    QuestionScoreResponse,
    ScheduleInterviewRequest,
    TranscriptTurnResponse,
)

router = APIRouter(prefix="/interviews", tags=["interviews"])


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _to_response(interview: Interview) -> InterviewResponse:
    base = get_settings().public_frontend_base
    return InterviewResponse(
        id=interview.id,
        candidate_name=interview.candidate_name,
        candidate_email=interview.candidate_email,
        role_title=interview.role_title,
        scheduled_at=interview.scheduled_at,
        status=interview.status,
        questions=interview.questions,
        transcript=[TranscriptTurnResponse(role=t.role, content=t.content) for t in interview.transcript],
        calendar_link=interview.calendar_link,
        overall_score=interview.overall_score,
        overall_verdict=interview.overall_verdict,  # type: ignore[arg-type]
        scores=[
            QuestionScoreResponse(question=s.question, answer=s.answer, score=s.score, justification=s.justification)
            for s in interview.scores
        ],
        has_report=interview.report_storage_key is not None,
        join_url=f"{base}/interview/{interview.access_token}",
    )


# --- Admin (authenticated) ---


@router.post("", response_model=InterviewResponse, status_code=201)
async def create_interview(
    body: ScheduleInterviewRequest, principal: AdminPrincipalDep, container: ContainerDep
) -> InterviewResponse:
    settings = get_settings()
    use_case = ScheduleInterview(
        container.unit_of_work(), container.llm, container.calendar, container.email,
        settings.public_frontend_base,
    )
    interview, calendar_created, email_sent = await use_case.execute(
        principal.tenant_id,
        candidate_name=body.candidate_name,
        candidate_email=body.candidate_email,
        role_title=body.role_title,
        job_document_id=DocumentId(body.job_document_id),
        resume_document_id=DocumentId(body.resume_document_id),
        scheduled_at=body.scheduled_at,
        custom_questions=body.custom_questions,
    )
    resp = _to_response(interview)
    resp.calendar_created = calendar_created
    resp.email_sent = email_sent
    return resp


@router.get("", response_model=list[InterviewResponse])
async def list_interviews(principal: AdminPrincipalDep, container: ContainerDep) -> list[InterviewResponse]:
    async with container.unit_of_work() as uow:
        uow.set_tenant_scope(principal.tenant_id)
        interviews = await uow.interviews.list_for_tenant(principal.tenant_id)
    return [_to_response(i) for i in interviews]


@router.get("/{interview_id}", response_model=InterviewResponse)
async def get_interview(
    interview_id: uuid.UUID, principal: AdminPrincipalDep, container: ContainerDep
) -> InterviewResponse:
    async with container.unit_of_work() as uow:
        uow.set_tenant_scope(principal.tenant_id)
        interview = await uow.interviews.get(principal.tenant_id, InterviewId(interview_id))
    if interview is None:
        raise HTTPException(status_code=404, detail="Interview not found")
    return _to_response(interview)


@router.get("/{interview_id}/report")
async def get_interview_report(
    interview_id: uuid.UUID, principal: AdminPrincipalDep, container: ContainerDep
) -> Response:
    async with container.unit_of_work() as uow:
        uow.set_tenant_scope(principal.tenant_id)
        interview = await uow.interviews.get(principal.tenant_id, InterviewId(interview_id))
    if interview is None or not interview.report_storage_key:
        raise HTTPException(status_code=404, detail="Report not available yet")
    data = await container.storage.get_bytes(interview.report_storage_key)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="interview-{interview_id}.pdf"'},
    )


# --- Candidate-facing (token-scoped, no auth) ---


async def _load_interview(container: ContainerDep, access_token: str) -> Interview:
    async with container.unit_of_work() as uow:
        interview = await uow.interviews.get_by_token(access_token)
    if interview is None:
        raise HTTPException(status_code=404, detail="Interview not found")
    return interview


async def _reference_texts(container: ContainerDep, interview: Interview) -> tuple[str, str]:
    async with container.unit_of_work() as uow:
        uow.set_tenant_scope(interview.tenant_id)
        job_chunks = await uow.chunks.list_for_document(interview.tenant_id, interview.job_document_id)
        resume_chunks = await uow.chunks.list_for_document(interview.tenant_id, interview.resume_document_id)
    return (
        "\n\n".join(c.text for c in job_chunks),
        "\n\n".join(c.text for c in resume_chunks),
    )


async def _check_quota(container: ContainerDep, interview: Interview) -> None:
    async with container.unit_of_work() as uow:
        uow.set_tenant_scope(interview.tenant_id)
        tenant = await uow.tenants.get(interview.tenant_id)
        used = await uow.usage.tokens_used_today(interview.tenant_id)
    if tenant and used >= tenant.daily_token_quota:
        raise HTTPException(status_code=429, detail="This organization's daily limit has been reached.")


@router.get("/token/{access_token}", response_model=InterviewBootstrapResponse)
async def get_interview_bootstrap(access_token: str, container: ContainerDep) -> InterviewBootstrapResponse:
    interview = await _load_interview(container, access_token)
    async with container.unit_of_work() as uow:
        uow.set_tenant_scope(interview.tenant_id)
        tenant = await uow.tenants.get(interview.tenant_id)
    return InterviewBootstrapResponse(
        candidate_name=interview.candidate_name,
        role_title=interview.role_title,
        tenant_name=tenant.name if tenant else "",
        scheduled_at=interview.scheduled_at,
        status=interview.status,
        can_join=interview.can_join(datetime.now(UTC)) or interview.status == "in_progress",
    )


@router.post("/token/{access_token}/greeting")
async def interview_greeting(access_token: str, request: Request, container: ContainerDep) -> EventSourceResponse:
    interview = await _load_interview(container, access_token)
    key = f"interview:{access_token}:{_client_ip(request)}"
    if not container.anon_rate_limiter.allow(key):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    if not interview.can_join(datetime.now(UTC)):
        raise HTTPException(status_code=400, detail="This interview cannot be started right now.")
    if not interview.questions:
        raise HTTPException(status_code=400, detail="This interview has no questions configured.")
    await _check_quota(container, interview)
    job_text, resume_text = await _reference_texts(container, interview)

    async def event_generator():  # type: ignore[no-untyped-def]
        instruction = (
            f"This is the very start of the interview. In 2-3 short sentences total, "
            f"warmly greet {interview.candidate_name or 'the candidate'}, briefly "
            f"mention you'll ask a series of questions about their background and "
            f"fit for the {interview.role_title or 'role'}, then ask this first "
            f"question: {interview.questions[0]}"
        )
        prompt = build_interview_turn_prompt(
            job_text=job_text, resume_text=resume_text, instruction=instruction,
            transcript_text=format_transcript(interview.transcript),
        )
        full: list[str] = []
        try:
            async for token in container.llm.stream(INTERVIEWER_SYSTEM_PROMPT, prompt):
                full.append(token)
                yield {"event": "token", "data": token}
        except Exception:  # noqa: BLE001 - end the stream; the frontend shows a retry state
            yield {"event": "error", "data": json.dumps({"detail": "Generation failed."})}
            return

        answer_text = "".join(full)
        tokens_used = max(1, (len(INTERVIEWER_SYSTEM_PROMPT) + len(prompt) + len(answer_text)) // 4)
        interview.status = "in_progress"
        interview.transcript.append(TranscriptTurn(role="assistant", content=answer_text))

        # Shielded — see the note in routers/chat.py's stream handler.
        async with shielded(), container.unit_of_work() as uow:
            uow.set_tenant_scope(interview.tenant_id)
            await uow.interviews.update(interview)
            await uow.usage.add_tokens(interview.tenant_id, tokens_used)
            await uow.commit()

        yield {"event": "done", "data": json.dumps({"completed": False})}

    return EventSourceResponse(event_generator())


@router.post("/token/{access_token}/respond")
async def interview_respond(
    access_token: str, body: AskRequest, request: Request, container: ContainerDep
) -> EventSourceResponse:
    interview = await _load_interview(container, access_token)
    key = f"interview:{access_token}:{_client_ip(request)}"
    if not container.anon_rate_limiter.allow(key):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    if interview.status != "in_progress":
        raise HTTPException(status_code=400, detail="This interview is not in progress.")
    await _check_quota(container, interview)
    job_text, resume_text = await _reference_texts(container, interview)

    input_verdict = scan_input(body.message)
    started = time.perf_counter()

    async def event_generator():  # type: ignore[no-untyped-def]
        interview.transcript.append(TranscriptTurn(role="user", content=body.message))

        if not input_verdict.allowed:
            yield {"event": "token", "data": GUARD_REFUSAL}
            async with shielded(), container.unit_of_work() as uow:
                uow.set_tenant_scope(interview.tenant_id)
                await uow.interviews.update(interview)
                await uow.commit()
            yield {"event": "done", "data": json.dumps({"completed": False})}
            return

        has_next = interview.advance()
        if has_next:
            instruction = (
                "Briefly and warmly acknowledge the candidate's last answer (one "
                f"short sentence), then ask this next question: {interview.current_question()}"
            )
        else:
            instruction = (
                "Briefly and warmly acknowledge the candidate's last answer, thank "
                "them for their time, and let them know this concludes the interview."
            )
        # transcript[:-1] excludes the answer just appended above — it's
        # passed separately as candidate_answer so the model sees it as "the
        # thing to react to now", not buried as the last line of history.
        prompt = build_interview_turn_prompt(
            job_text=job_text, resume_text=resume_text, instruction=instruction, candidate_answer=body.message,
            transcript_text=format_transcript(interview.transcript[:-1]),
        )
        full: list[str] = []
        try:
            async for token in container.llm.stream(INTERVIEWER_SYSTEM_PROMPT, prompt):
                full.append(token)
                yield {"event": "token", "data": token}
        except Exception:  # noqa: BLE001 - end the stream; the frontend shows a retry state
            yield {"event": "error", "data": json.dumps({"detail": "Generation failed."})}
            return

        answer_text = "".join(full)
        interview.transcript.append(TranscriptTurn(role="assistant", content=answer_text))
        tokens_used = max(1, (len(INTERVIEWER_SYSTEM_PROMPT) + len(prompt) + len(answer_text)) // 4)

        # Shielded — see the note in routers/chat.py's stream handler.
        async with shielded(), container.unit_of_work() as uow:
            uow.set_tenant_scope(interview.tenant_id)
            await uow.interviews.update(interview)
            await uow.usage.add_tokens(interview.tenant_id, tokens_used)
            await uow.commit()

        completed_now = False
        if not has_next:
            # Also shielded: this is the interview's finalize/report step, not
            # merely a log — losing it to a disconnect right on the last turn
            # would leave a completed interview stuck looking "in progress".
            with anyio.CancelScope(shield=True):
                finalize = FinalizeInterview(
                    container.unit_of_work(), container.llm, container.storage
                )
                await finalize.execute(interview.tenant_id, interview)
            completed_now = True

        yield {
            "event": "done",
            "data": json.dumps({"completed": completed_now, "elapsed_ms": int((time.perf_counter() - started) * 1000)}),
        }

    return EventSourceResponse(event_generator())
