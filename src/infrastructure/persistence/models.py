"""SQLAlchemy 2 ORM models.

These are persistence-layer types, distinct from domain entities. Repositories
map between the two so the domain never depends on SQLAlchemy. The chunk
embedding column uses pgvector for similarity search inside Postgres.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.config import get_settings
from src.infrastructure.persistence.database import Base

EMBEDDING_DIM = get_settings().embedding_dim


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TenantModel(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    daily_token_quota: Mapped[int] = mapped_column(Integer, default=2_000_000)
    max_documents: Mapped[int] = mapped_column(Integer, default=200)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class UserModel(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="owner")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Platform-wide role, orthogonal to the tenant-scoped `role` above. Grants
    # cross-tenant visibility in the Super Admin panel — see migration 0036.
    is_platform_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ApiKeyModel(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    key_hash: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(16))
    # The scopes the plan granted when this key was minted (migration 0034).
    # JSONB rather than ARRAY(String) to match how every other list-shaped
    # column in this schema is stored, and so a scope can grow structure later
    # without a type change.
    scopes: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    plan_tier: Mapped[str] = mapped_column(String(20), default="free", server_default="free")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SubscriptionModel(Base):
    """One row per paying tenant (migration 0034). A tenant with no row is on
    the free tier — see `Subscription.free()`."""

    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    tier: Mapped[str] = mapped_column(String(20), default="free", server_default="free")
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class BillingTransactionModel(Base):
    """Append-only billing history (migration 0034)."""

    __tablename__ = "billing_transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20), index=True)
    amount_usd: Mapped[float] = mapped_column(Float, default=0.0)
    description: Mapped[str] = mapped_column(String(255), default="")
    plan_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ApiUsageDailyModel(Base):
    """API calls made with a key, bucketed by day (migration 0034).

    Per day rather than per request: the monthly allowance is the only thing
    this number feeds, and one row per call would make the metering table the
    largest in the database to answer a question that is a SUM over 30 rows.
    """

    __tablename__ = "api_usage_daily"
    __table_args__ = (UniqueConstraint("tenant_id", "day", name="uq_api_usage_tenant_day"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    day: Mapped[date] = mapped_column(Date, index=True)
    calls: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class TenantInviteModel(Base):
    __tablename__ = "tenant_invites"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(255), index=True)
    role: Mapped[str] = mapped_column(String(20), default="member")
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DocumentModel(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(String(512))
    checksum: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    chunks: Mapped[list[ChunkModel]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )


class ChunkModel(Base):
    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)

    document: Mapped[DocumentModel] = relationship(back_populates="chunks")


class ChatbotModel(Base):
    __tablename__ = "chatbots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    # Short, human-quotable id shown in the UI as #236637. Assigned by a
    # Postgres sequence (migration 0020), never by the application.
    display_id: Mapped[int] = mapped_column(
        Integer, server_default=text("nextval('chatbot_display_id_seq')"), unique=True
    )
    channel: Mapped[str] = mapped_column(String(16), default="text")
    system_prompt: Mapped[str] = mapped_column(Text)
    # Ordered [{id, title, body, enabled}] — the authored form of system_prompt.
    # Empty list = the owner wrote a raw prompt instead of using the flow editor.
    flow_sections: Mapped[list] = mapped_column(JSONB, default=list)
    retrieval: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Runtime settings shown on the Assistant Details tab: call direction,
    # languages, TTS/LLM/STT choices, and the welcome message. One blob — see
    # migration 0017 for why it isn't a column each.
    assistant_config: Mapped[dict] = mapped_column(JSONB, default=dict)
    allowed_document_ids: Mapped[list] = mapped_column(JSONB, default=list)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    # Publishable key embedded in the widget snippet; looked up across tenants.
    public_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    allowed_origins: Mapped[list] = mapped_column(JSONB, default=list)
    widget_config: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Cloned voice used for spoken replies. NULL = fall back to the browser's
    # built-in speech synthesis. SET NULL on delete so removing a voice degrades
    # the assistant to the default voice instead of deleting the assistant.
    voice_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("voice_profiles.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ChatSessionModel(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # Nullable since 0023: a WhatsApp thread exists from the first inbound
    # message, which can arrive before anyone has chosen who answers it.
    chatbot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chatbots.id", ondelete="CASCADE"), nullable=True
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # Added in 0031: the booking agent's working memory for this thread — the
    # numbered times it last offered, which one the customer picked, the hold
    # token, and the details already given. Lives on the session rather than in
    # a table of its own because its lifetime IS the session's: one row, deleted
    # by the same cascade, and read on every inbound message. NULL for every
    # conversation that never mentions an appointment.
    booking_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class ChatMessageModel(Base):
    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[list] = mapped_column(JSONB, default=list)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # WhatsApp attachments. Separate columns rather than riding in `citations`,
    # which the mapper reads as Citation dicts with unguarded key access.
    media_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    media_mime_type: Mapped[str | None] = mapped_column(String(160), nullable=True)
    media_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    media_storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    media_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # WhatsApp's message id, for deduplicating socket redeliveries on reconnect.
    provider_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)


class RagRequestLogModel(Base):
    """One row per chatbot request — the per-request evaluation/provenance log.

    Written for EVERY ask, success or failure (a failed generation that never
    produces an assistant message still lands here), so 'every request is logged'
    holds even when the answer path throws. Separate from chat_messages, which
    only records successful turns of the conversation.
    """

    __tablename__ = "rag_request_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    chatbot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    query: Mapped[str] = mapped_column(Text)
    # Candidate chunks with scores — the retrieval trace for this request.
    retrieved: Mapped[list] = mapped_column(JSONB, default=list)
    num_retrieved: Mapped[int] = mapped_column(Integer, default=0)
    max_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    no_context: Mapped[bool] = mapped_column(Boolean, default=False)
    refused: Mapped[bool] = mapped_column(Boolean, default=False)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(10), default="ok", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


class UsageCounterModel(Base):
    """Daily per-tenant token usage. Atomic upserts replace Redis counters."""

    __tablename__ = "usage_counters"
    __table_args__ = (UniqueConstraint("tenant_id", "day", name="uq_usage_tenant_day"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)


class InterviewModel(Base):
    __tablename__ = "interviews"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    candidate_name: Mapped[str] = mapped_column(String(200), default="")
    candidate_email: Mapped[str] = mapped_column(String(320))
    role_title: Mapped[str] = mapped_column(String(200), default="")
    job_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE")
    )
    resume_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE")
    )
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="scheduled", index=True)
    access_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    questions: Mapped[list] = mapped_column(JSONB, default=list)
    transcript: Mapped[list] = mapped_column(JSONB, default=list)
    current_question_index: Mapped[int] = mapped_column(Integer, default=0)
    google_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    calendar_link: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    report_storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    overall_verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scores: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class InterviewBatchModel(Base):
    __tablename__ = "interview_batches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    role_title: Mapped[str] = mapped_column(String(200), default="")
    job_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE")
    )
    window_opens_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    custom_questions: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(20), default="collecting", index=True)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class BatchCandidateModel(Base):
    __tablename__ = "interview_batch_candidates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("interview_batches.id", ondelete="CASCADE"), index=True
    )
    resume_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE")
    )
    resume_filename: Mapped[str] = mapped_column(String(500), default="")
    candidate_name: Mapped[str] = mapped_column(String(200), default="")
    candidate_email: Mapped[str] = mapped_column(String(320), default="")
    status: Mapped[str] = mapped_column(String(20), default="ingesting", index=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    interview_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("interviews.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class OAuthConnectionModel(Base):
    """One tenant's consent to one OAuth provider.

    Keyed (tenant_id, provider) so every consent-based integration shares this
    table — `provider` matches an id in infrastructure/oauth/providers.
    """

    __tablename__ = "oauth_connections"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scope: Mapped[str] = mapped_column(String(1000), default="")
    account_label: Mapped[str] = mapped_column(String(320), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class WhatsAppChannelModel(Base):
    __tablename__ = "whatsapp_channels"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    chatbot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chatbots.id", ondelete="CASCADE"), unique=True
    )
    phone_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    # "twilio" | "cloud" (Meta WhatsApp Cloud API) — see WhatsAppChannel.
    provider: Mapped[str] = mapped_column(String(16), server_default="twilio")
    twilio_account_sid: Mapped[str] = mapped_column(String(64), server_default="")
    twilio_auth_token: Mapped[str] = mapped_column(Text, server_default="")
    # Cloud API only. `phone_number_id` is how an inbound webhook resolves to a
    # tenant, so migration 0029 indexes it uniquely wherever it is non-empty.
    phone_number_id: Mapped[str] = mapped_column(String(64), server_default="")
    waba_id: Mapped[str] = mapped_column(String(64), server_default="")
    access_token: Mapped[str] = mapped_column(Text, server_default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class WhatsAppConversationModel(Base):
    __tablename__ = "whatsapp_conversations"
    __table_args__ = (
        UniqueConstraint("whatsapp_channel_id", "phone_number", name="uq_whatsapp_conv_channel_phone"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    # Holds EITHER a whatsapp_channels.id (Cloud API) or a
    # whatsapp_web_sessions.id (QR-linked personal account), so it carries no
    # foreign key — migration 0021 drops the one that was rejecting every
    # personal-WhatsApp conversation.
    whatsapp_channel_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    phone_number: Mapped[str] = mapped_column(String(32))
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE")
    )
    # False for announce-only campaigns: the reply is still recorded, the
    # assistant just does not answer it.
    auto_reply: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # Added in 0022. The table originally had no tenant of its own — it was
    # scoped through its channel — but the inbox queries it directly, so it
    # needs to be scopable and RLS-guarded on its own.
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    # Denormalized so the thread list renders in one query instead of a message
    # lookup per conversation.
    display_name: Mapped[str] = mapped_column(String(160), server_default="")
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_message_preview: Mapped[str] = mapped_column(String(300), server_default="")
    unread_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    has_attachment: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Added in 0024. NULL means nobody is being waited on, which is the state
    # the follow-up sweep's partial index is built to skip.
    awaiting_reply_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    followups_sent: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # --- Shared-inbox working state (0026) ---------------------------------
    # Who owns this thread. SET NULL on the FK: a teammate leaving unassigns
    # their conversations, it never deletes them.
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    tags: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    pinned: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # "open" | "closed" — see migration 0026 for why this is not a boolean.
    status: Mapped[str] = mapped_column(String(16), server_default="open")
    # --- Contact card (0026) ------------------------------------------------
    # WhatsApp gives us a number and, if they have one set, a pushname. Every
    # other fact about the person is something an operator learns and types, so
    # it lives here rather than being re-derived per view.
    company: Mapped[str] = mapped_column(String(160), server_default="")
    job_title: Mapped[str] = mapped_column(String(120), server_default="")
    email: Mapped[str] = mapped_column(String(254), server_default="")
    city: Mapped[str] = mapped_column(String(120), server_default="")
    country: Mapped[str] = mapped_column(String(120), server_default="")
    linkedin_url: Mapped[str] = mapped_column(String(300), server_default="")
    source: Mapped[str] = mapped_column(String(60), server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class WhatsAppConversationNoteModel(Base):
    """An internal note on a thread — what the team tells each other about a
    contact, never sent to them.

    Separate from `chat_messages` on purpose: a note must never be able to
    reach WhatsApp, and the surest way to guarantee that is for it not to live
    in the table the send path reads.
    """

    __tablename__ = "whatsapp_conversation_notes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("whatsapp_conversations.id", ondelete="CASCADE"),
        index=True,
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Kept alongside the id so the note still says who wrote it once that
    # account is gone — an audit line that degrades to "someone" is not one.
    author_email: Mapped[str] = mapped_column(String(254), server_default="")
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AuditEventModel(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    name: Mapped[str] = mapped_column(String(80), index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PostCallConfigModel(Base):
    __tablename__ = "post_call_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    chatbot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chatbots.id", ondelete="CASCADE"), index=True
    )
    delivery_method: Mapped[str] = mapped_column(String(20), default="webhook")
    webhook_url: Mapped[str] = mapped_column(Text, default="")
    email_to: Mapped[str] = mapped_column(String(320), default="")
    trigger_statuses: Mapped[list] = mapped_column(JSONB, default=list)
    include_summary: Mapped[bool] = mapped_column(Boolean, default=True)
    include_transcript: Mapped[bool] = mapped_column(Boolean, default=True)
    include_sentiment: Mapped[bool] = mapped_column(Boolean, default=False)
    include_extracted: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PostCallDeliveryModel(Base):
    __tablename__ = "post_call_deliveries"
    __table_args__ = (
        UniqueConstraint("config_id", "session_id", name="uq_post_call_delivery_config_session"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    chatbot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chatbots.id", ondelete="CASCADE"), index=True
    )
    # Intentionally not a FK — the audit row outlives the config it ran under.
    config_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    call_status: Mapped[str] = mapped_column(String(20))
    delivery_method: Mapped[str] = mapped_column(String(20))
    destination: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BroadcastModel(Base):
    __tablename__ = "broadcasts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    chatbot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chatbots.id", ondelete="CASCADE"), index=True
    )
    # Exactly one of these is set, per `sender_kind` — see migration 0021.
    whatsapp_channel_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("whatsapp_channels.id", ondelete="CASCADE"), nullable=True
    )
    whatsapp_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("whatsapp_web_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    sender_kind: Mapped[str] = mapped_column(String(16), server_default="cloud_api")
    mode: Mapped[str] = mapped_column(String(20), server_default="broadcast_reply")
    name: Mapped[str] = mapped_column(String(160))
    message_template: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    delivered_count: Mapped[int] = mapped_column(Integer, default=0)
    read_count: Mapped[int] = mapped_column(Integer, default=0)
    replied_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class BroadcastRecipientModel(Base):
    __tablename__ = "broadcast_recipients"
    __table_args__ = (
        UniqueConstraint("broadcast_id", "phone_number", name="uq_broadcast_recipient_phone"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    broadcast_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("broadcasts.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    phone_number: Mapped[str] = mapped_column(String(32))
    display_name: Mapped[str] = mapped_column(String(160), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error: Mapped[str] = mapped_column(Text, default="")
    provider_message_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="SET NULL"), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class TenantIntegrationModel(Base):
    __tablename__ = "tenant_integrations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "integration_id", name="uq_tenant_integration"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # Catalogue key (e.g. "slack"), not a FK — the catalogue ships with the code.
    integration_id: Mapped[str] = mapped_column(String(64), index=True)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class IssueReportModel(Base):
    __tablename__ = "issue_reports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(320))
    phone: Mapped[str] = mapped_column(String(32), default="")
    report_type: Mapped[str] = mapped_column(String(32), index=True)
    priority: Mapped[str] = mapped_column(String(16), default="medium", index=True)
    subject: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    page_url: Mapped[str] = mapped_column(String(500), default="")
    user_agent: Mapped[str] = mapped_column(String(500), default="")
    email_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class VoiceProfileModel(Base):
    __tablename__ = "voice_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(80))
    gender: Mapped[str] = mapped_column(String(16), default="female")
    language: Mapped[str] = mapped_column(String(8), default="en")
    description: Mapped[str] = mapped_column(String(500), default="")
    sample_storage_key: Mapped[str] = mapped_column(String(512), default="")
    sample_content_type: Mapped[str] = mapped_column(String(120), default="")
    sample_bytes: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    provider: Mapped[str] = mapped_column(String(32), default="")
    provider_voice_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class WhatsAppWebSessionModel(Base):
    __tablename__ = "whatsapp_web_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # SET NULL: deleting an assistant must not unlink the user's WhatsApp.
    chatbot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chatbots.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    phone_number: Mapped[str] = mapped_column(String(32), default="", index=True)
    display_name: Mapped[str] = mapped_column(String(160), default="")
    qr_data_url: Mapped[str] = mapped_column(Text, default="")
    qr_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str] = mapped_column(Text, default="")
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


# --- Scheduling (migration 0025) --------------------------------------------
#
# The appointment engine. Every table here is tenant-scoped and RLS-guarded like
# the rest of the schema. The one that carries the design is
# `ResourceReservationModel` — see its docstring.


class LocationModel(Base):
    __tablename__ = "locations"
    __table_args__ = (UniqueConstraint("tenant_id", "slug", name="uq_location_tenant_slug"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    # URL-safe handle, reserved for the public booking pages of a later phase.
    # Unique per tenant, not globally: two businesses may both have a "downtown".
    slug: Mapped[str] = mapped_column(String(80))
    # IANA zone name. The single most load-bearing column in this module: every
    # weekly availability rule is resolved against it.
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    address: Mapped[str] = mapped_column(Text, default="")
    phone: Mapped[str] = mapped_column(String(32), default="")
    email: Mapped[str] = mapped_column(String(320), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ServiceModel(Base):
    __tablename__ = "services"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(160), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    duration_minutes: Mapped[int] = mapped_column(Integer)
    buffer_before_minutes: Mapped[int] = mapped_column(Integer, default=0)
    buffer_after_minutes: Mapped[int] = mapped_column(Integer, default=0)
    # Money in minor units. Never a float — a rounded price is a support ticket.
    price_cents: Mapped[int] = mapped_column(Integer, default=0)
    deposit_cents: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="AED")
    min_notice_minutes: Mapped[int] = mapped_column(Integer, default=0)
    max_horizon_days: Mapped[int] = mapped_column(Integer, default=60)
    cancellation_window_hours: Mapped[int] = mapped_column(Integer, default=0)
    online_bookable: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ResourceModel(Base):
    """Staff, rooms, equipment, vehicles — one table, discriminated by `kind`.

    Modelling staff separately is what stops a scheduler ever booking a meeting
    room, so the availability engine treats every row here identically.
    """

    __tablename__ = "resources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(20), default="staff", index=True)
    # SET NULL: closing a branch must not delete the doctors who worked there.
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("locations.id", ondelete="SET NULL"), nullable=True
    )
    # SET NULL for the same reason in reverse: removing someone's login must not
    # delete the resource their appointments point at.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    email: Mapped[str] = mapped_column(String(320), default="")
    phone: Mapped[str] = mapped_column(String(32), default="")
    capacity: Mapped[int] = mapped_column(Integer, default=1)
    # Blank = inherit the location's zone, which is the usual case.
    timezone: Mapped[str] = mapped_column(String(64), default="")
    color: Mapped[str] = mapped_column(String(16), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ServiceResourceModel(Base):
    """Which resources can serve which service, and in what role.

    `role` is what makes multi-resource booking work: a consultation requiring
    one "practitioner" and one "room" is two rows with different roles, and the
    availability engine fills every distinct required role before offering a slot.
    """

    __tablename__ = "service_resources"
    __table_args__ = (
        UniqueConstraint(
            "service_id", "resource_id", "role", name="uq_service_resource_role"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id", ondelete="CASCADE"), index=True
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resources.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(40), default="primary")
    required: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AvailabilityRuleModel(Base):
    """A recurring weekly window, stored as LOCAL wall-clock time.

    `start_time`/`end_time` are `Time` without a zone on purpose. "Open Mondays
    09:00" must stay 09:00 through a daylight-saving change; storing the UTC
    instant would move every branch by an hour twice a year. The zone comes from
    the owning location (or resource) at query time.
    """

    __tablename__ = "availability_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # "location" | "resource". Polymorphic rather than two nullable FKs so the
    # engine can load every rule for a query in one indexed statement.
    owner_kind: Mapped[str] = mapped_column(String(16))
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    # Monday = 0, matching `datetime.weekday()`.
    weekday: Mapped[int] = mapped_column(Integer)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    effective_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    effective_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BlockedPeriodModel(Base):
    """Leave, holidays, maintenance — absolute UTC intervals, not recurrences.

    These genuinely are one-off ("that Tuesday off"), which is why they are
    instants here while `availability_rules` are wall-clock.
    """

    __tablename__ = "blocked_periods"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    owner_kind: Mapped[str] = mapped_column(String(16))
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AppointmentModel(Base):
    """The canonical booking. Every channel writes one of these and nothing else."""

    __tablename__ = "appointments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # RESTRICT, not CASCADE: deleting a branch or a service must not silently
    # erase the appointments booked against it. The UI deactivates instead.
    location_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("locations.id", ondelete="RESTRICT"), index=True
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id", ondelete="RESTRICT"), index=True
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Customer identity as columns, not a FK: this phase has no CRM entity yet.
    # Named to match the one that will replace them so the later change is a
    # backfill rather than a redesign.
    customer_name: Mapped[str] = mapped_column(String(160))
    customer_phone: Mapped[str] = mapped_column(String(32), default="", index=True)
    customer_email: Mapped[str] = mapped_column(String(320), default="")
    customer_timezone: Mapped[str] = mapped_column(String(64), default="")
    # Copied from the location at booking time, so correcting a branch's zone
    # later cannot retroactively move appointments that already happened.
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    # NULL until the pre-appointment reminder has gone out (0028). The
    # sweep's partial index covers exactly the NULL rows.
    reminder_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Channel attribution (spec section 44). Never changes after creation.
    source: Mapped[str] = mapped_column(String(20), default="staff", index=True)
    # Denormalized copy of the reserved resources, so rendering a calendar does
    # not need a join per appointment. `resource_reservations` remains the
    # authority on what is actually booked.
    resource_ids: Mapped[list] = mapped_column(JSONB, default=list)
    customer_notes: Mapped[str] = mapped_column(Text, default="")
    internal_notes: Mapped[str] = mapped_column(Text, default="")
    rescheduled_from_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True
    )
    cancellation_reason: Mapped[str] = mapped_column(String(500), default="")
    # Empty string rather than NULL is NOT usable here: the unique index below is
    # partial on non-empty values, so unkeyed bookings never collide.
    idempotency_key: Mapped[str] = mapped_column(String(128), default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class AppointmentStatusHistoryModel(Base):
    """Append-only record of every status change (spec section 40).

    Never updated and never deleted with the appointment intact: this is the
    answer to "who cancelled this, and when".
    """

    __tablename__ = "appointment_status_history"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    appointment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id", ondelete="CASCADE"), index=True
    )
    from_status: Mapped[str] = mapped_column(String(24))
    to_status: Mapped[str] = mapped_column(String(24))
    # "staff" | "customer" | "ai_agent" | "system". Deliberately distinct from
    # the appointment's `source`: an AI can create what a receptionist cancels.
    actor_kind: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor_label: Mapped[str] = mapped_column(String(160), default="")
    channel: Mapped[str] = mapped_column(String(32), default="")
    reason: Mapped[str] = mapped_column(String(500), default="")
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


class ResourceReservationModel(Base):
    """Claimed time on one resource — and the double-booking guard itself.

    Holds and bookings are rows in the SAME table so they compete for the same
    time on the same constraint. Migration 0025 adds:

        EXCLUDE USING gist (
            resource_id WITH =,
            (tstzrange(starts_at, ends_at, '[)')) WITH &&
        ) WHERE (released_at IS NULL)

    which is the whole of spec section 12. Two customers booking 3:00 PM at the
    same instant cannot both succeed regardless of transaction interleaving or
    how many web workers are running: Postgres rejects the loser with a
    constraint violation the use case turns into a 409 plus fresh slots. This is
    strictly stronger than SELECT ... FOR UPDATE (which cannot lock a row that
    does not exist yet) and needs no lock ordering.

    The range is an expression over two ordinary timestamp columns rather than a
    stored `tstzrange`, so the ORM writes plain datetimes and no driver-specific
    range type is involved.

    `released_at` rather than DELETE: a cancelled booking's reservation stays as
    a record of what was held, while dropping out of the constraint's scope.
    """

    __tablename__ = "resource_reservations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resources.id", ondelete="CASCADE"), index=True
    )
    # Includes the service's buffers — this is the block of calendar consumed,
    # which is wider than the appointment the customer sees.
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # "hold" | "booking".
    kind: Mapped[str] = mapped_column(String(10), index=True)
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id", ondelete="CASCADE"), nullable=True
    )
    # Unguessable handle returned to whoever created the hold; presenting it is
    # what converts the hold into a booking.
    hold_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Set for holds only. A booking never expires.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class OnboardingPrefsModel(Base):
    """What one person has already been shown (migration 0033).

    Preferences only. Setup milestones are derived from the tables that own
    them on every read, never mirrored here — see `OnboardingReadModel`.
    """

    __tablename__ = "onboarding_prefs"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # "guided" | "full" — only ever changed by the person clicking "Show all
    # features", never by the app reacting to progress.
    nav_mode: Mapped[str] = mapped_column(String(16), default="guided")
    tours_completed: Mapped[list] = mapped_column(JSONB, default=list)
    dismissed: Mapped[list] = mapped_column(JSONB, default=list)
    celebrated_stages: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
