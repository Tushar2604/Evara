"""Use-case input/output DTOs (Pydantic v2).

These are the typed contracts between the interface layer and use cases. They
are deliberately separate from both domain entities and API schemas so each can
evolve independently.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class RegisterTenantInput(BaseModel):
    tenant_name: str = Field(min_length=2, max_length=120)
    owner_email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class AuthOutput(BaseModel):
    access_token: str
    refresh_token: str
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    is_platform_admin: bool = False


class CreateUploadInput(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str
    size_bytes: int = Field(gt=0)


class CreateUploadOutput(BaseModel):
    document_id: uuid.UUID
    upload_url: str
    storage_key: str


class AskInput(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    # WhatsApp stores the inbound message itself, because it must be kept even
    # when no assistant answers it and because it may carry an attachment this
    # use case knows nothing about. Letting it be re-added here would show every
    # such message twice in the inbox.
    persist_user_message: bool = True
    # Answer as this assistant rather than the one the session was opened with.
    # WhatsApp needs it: a thread is created at the first inbound message and
    # then outlives every later change to which assistant answers the number,
    # so the session's own chatbot is stale the moment the user picks another.
    chatbot_id: uuid.UUID | None = None


class CitationOut(BaseModel):
    document_id: uuid.UUID
    chunk_id: str
    ordinal: int
    score: float
    snippet: str


class AnswerOutput(BaseModel):
    message_id: uuid.UUID
    answer: str
    citations: list[CitationOut]
    tokens_used: int
    provider: str


class AgentAskInput(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    # Cap on agent reasoning steps for this request (server still hard-bounds it).
    max_steps: int = Field(default=6, ge=1, le=12)


class AgentStepOut(BaseModel):
    index: int
    thought: str
    action: str
    observation: str
    model: str


class AgentAnswerOutput(BaseModel):
    """Agent answer plus the trace — the trace is part of the contract so callers
    (and the eval/observability tooling) can inspect how the answer was reached."""

    answer: str
    citations: list[CitationOut]
    tokens_used: int
    provider: str | None
    stop_reason: str
    tools_used: list[str]
    steps: list[AgentStepOut]


class ScheduleInterviewInput(BaseModel):
    candidate_name: str = Field(default="", max_length=200)
    candidate_email: EmailStr
    role_title: str = Field(default="", max_length=200)
    job_document_id: uuid.UUID
    resume_document_id: uuid.UUID
    scheduled_at: datetime


class ScheduleInterviewOutput(BaseModel):
    interview_id: uuid.UUID
    candidate_name: str
    join_url: str
    calendar_created: bool
    email_sent: bool
