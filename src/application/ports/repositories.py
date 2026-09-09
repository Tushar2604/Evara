"""Repository ports (Repository Pattern).

These are the seams between the application core and persistence. Use cases
depend only on these Protocols; the infrastructure layer provides SQLAlchemy
implementations. Every method is tenant-scoped — isolation is enforced here and
again by Postgres RLS as a backstop.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Protocol, runtime_checkable

from src.domain.billing.entities import BillingTransaction, Subscription
from src.domain.broadcast.entities import Broadcast, BroadcastRecipient
from src.domain.chat.entities import ChatSession, Message
from src.domain.chatbot.entities import Chatbot
from src.domain.document.entities import Chunk, Document
from src.domain.integration.entities import TenantIntegration
from src.domain.interview.batch_entities import BatchCandidate, InterviewBatch
from src.domain.interview.entities import Interview
from src.domain.onboarding.entities import Milestones, OnboardingPrefs
from src.domain.postcall.entities import PostCallConfig, PostCallDelivery
from src.domain.scheduling.availability import Interval
from src.domain.scheduling.entities import (
    Appointment,
    AvailabilityRule,
    BlockedPeriod,
    Location,
    Resource,
    Service,
    ServiceResource,
    StatusChange,
)
from src.domain.shared.identifiers import (
    AppointmentId,
    AvailabilityRuleId,
    BatchCandidateId,
    BlockedPeriodId,
    ChatbotId,
    DocumentId,
    InterviewBatchId,
    InterviewId,
    LocationId,
    MessageId,
    ResourceId,
    ServiceId,
    SessionId,
    TenantId,
    UserId,
    new_id,
)
from src.domain.support.entities import IssueReport
from src.domain.tenant.entities import ApiKey, Tenant, User
from src.domain.voice.entities import VoiceProfile
from src.domain.whatsapp_web.entities import WhatsAppWebSession


@runtime_checkable
class TenantRepository(Protocol):
    async def add(self, tenant: Tenant) -> None: ...
    async def get(self, tenant_id: TenantId) -> Tenant | None: ...
    async def get_by_slug(self, slug: str) -> Tenant | None: ...
    async def set_limits(
        self, tenant_id: TenantId, *, daily_token_quota: int, max_documents: int
    ) -> None:
        """Apply a plan's ceilings to the tenant row.

        The token quota and document ceiling were already columns on
        `tenants`, and the ingestion and answering paths already read them.
        Rather than teach every one of those call sites about plans, a plan
        change writes its numbers here — so the limits a customer is sold
        are the same ones the request path enforces, with no second
        implementation to drift."""
        ...

    async def set_active(self, tenant_id: TenantId, is_active: bool) -> None: ...


@runtime_checkable
class UserRepository(Protocol):
    async def add(self, user: User) -> None: ...
    async def get(self, user_id: UserId) -> User | None: ...
    async def get_by_email(self, email: str) -> User | None: ...
    async def set_password_hash(self, user_id: UserId, password_hash: str) -> None: ...
    async def list_for_tenant(self, tenant_id: TenantId) -> list[User]: ...
    async def touch_login(self, user_id: UserId, when: datetime) -> None: ...
    async def set_active(self, user_id: UserId, is_active: bool) -> None: ...
    async def set_platform_admin(self, user_id: UserId, is_platform_admin: bool) -> None: ...


@runtime_checkable
class ApiKeyRepository(Protocol):
    async def add(self, key: ApiKey) -> None: ...
    async def get_by_hash(self, key_hash: str) -> ApiKey | None: ...
    async def get(self, tenant_id: TenantId, key_id: uuid.UUID) -> ApiKey | None: ...
    async def list_for_tenant(self, tenant_id: TenantId) -> list[ApiKey]: ...
    async def revoke(self, tenant_id: TenantId, key_id: uuid.UUID) -> bool: ...
    async def touch(self, key_id: uuid.UUID) -> None: ...


@runtime_checkable
class SubscriptionRepository(Protocol):
    """The tenant's plan. `get` returns None for a workspace that has never
    paid — callers resolve that to `Subscription.free(tenant_id)` rather than
    the repository inventing a row (see the entity's docstring)."""

    async def get(self, tenant_id: TenantId) -> Subscription | None: ...
    async def upsert(self, subscription: Subscription) -> None: ...


@runtime_checkable
class BillingTransactionRepository(Protocol):
    async def add(self, transaction: BillingTransaction) -> None: ...
    async def list_for_tenant(
        self, tenant_id: TenantId, limit: int = 50
    ) -> list[BillingTransaction]: ...


@runtime_checkable
class ApiUsageRepository(Protocol):
    """The meter behind a plan's monthly API allowance."""

    async def record_call(self, tenant_id: TenantId) -> None: ...
    async def calls_since(self, tenant_id: TenantId, since: date) -> int: ...


def generate_invite_token() -> str:
    """An unguessable invite token — safe to embed in an emailed link,
    analogous to an interview's access_token or a chatbot's publishable key."""
    return secrets.token_urlsafe(24)


@dataclass
class TenantInvite:
    """An Owner/Admin's invitation for a teammate to join their tenant with a
    chosen role. The teammate has no account yet — `token` is emailed as a
    link; possessing it is what lets them set their own password and create
    their User row. `expires_at` is set by the inviting use case (a fixed
    window from creation), not defaulted here."""

    tenant_id: TenantId
    email: str
    role: str  # Role value ("admin" | "member" | "viewer") chosen by the inviter
    expires_at: datetime
    id: uuid.UUID = field(default_factory=new_id)
    token: str = field(default_factory=generate_invite_token)
    status: str = "pending"  # "pending" | "accepted"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@runtime_checkable
class TenantInviteRepository(Protocol):
    async def add(self, invite: TenantInvite) -> None: ...
    async def get_by_token(self, token: str) -> TenantInvite | None: ...
    async def list_for_tenant(self, tenant_id: TenantId) -> list[TenantInvite]: ...
    async def mark_accepted(self, invite: TenantInvite) -> None: ...


@runtime_checkable
class DocumentRepository(Protocol):
    async def add(self, document: Document) -> None: ...
    async def get(self, tenant_id: TenantId, document_id: DocumentId) -> Document | None: ...
    async def update(self, document: Document) -> None: ...
    async def delete(self, tenant_id: TenantId, document_id: DocumentId) -> None: ...
    async def list_for_tenant(self, tenant_id: TenantId) -> list[Document]: ...
    async def count_for_tenant(self, tenant_id: TenantId) -> int: ...
    async def list_resumable(self) -> list[Document]:
        """Non-terminal documents to resume after a process restart/sleep."""
        ...


@runtime_checkable
class ChunkRepository(Protocol):
    """Vector store port, backed by pgvector."""

    async def add_many(self, chunks: list[Chunk], embeddings: list[list[float]]) -> None: ...
    async def delete_for_document(self, tenant_id: TenantId, document_id: DocumentId) -> None: ...
    async def list_for_document(
        self, tenant_id: TenantId, document_id: DocumentId
    ) -> list[Chunk]:
        """All chunks for one document, in ordinal order — reconstructs full
        text. Used where a small, fixed document set (e.g. one resume + one
        job description) should be passed as complete context rather than
        top-k retrieved."""
        ...
    async def search(
        self,
        tenant_id: TenantId,
        query_embedding: list[float],
        top_k: int,
        document_ids: list[DocumentId] | None = None,
        min_score: float = 0.0,
    ) -> list[tuple[Chunk, float]]:
        """Return (chunk, similarity) pairs, tenant-filtered."""
        ...


@runtime_checkable
class ChatbotRepository(Protocol):
    async def add(self, chatbot: Chatbot) -> None: ...
    async def get(self, tenant_id: TenantId, chatbot_id: ChatbotId) -> Chatbot | None: ...
    async def get_public(self, chatbot_id: ChatbotId) -> Chatbot | None: ...
    async def get_by_public_key(self, public_key: str) -> Chatbot | None: ...
    async def update(self, chatbot: Chatbot) -> None: ...
    async def delete(self, tenant_id: TenantId, chatbot_id: ChatbotId) -> None: ...
    async def list_for_tenant(self, tenant_id: TenantId) -> list[Chatbot]: ...


@runtime_checkable
class ChatRepository(Protocol):
    async def add_session(self, session: ChatSession) -> None: ...
    async def get_session(
        self, tenant_id: TenantId, session_id: SessionId
    ) -> ChatSession | None: ...
    async def add_message(self, message: Message) -> None: ...
    async def list_messages(
        self,
        tenant_id: TenantId,
        session_id: SessionId,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Message]: ...
    async def add_messages(self, messages: list[Message]) -> None: ...
    async def existing_provider_ids(
        self, tenant_id: TenantId, session_id: SessionId, provider_ids: list[str]
    ) -> set[str]: ...
    async def count_messages(self, tenant_id: TenantId, session_id: SessionId) -> int: ...
    async def message_counts(
        self, tenant_id: TenantId, session_ids: list[SessionId]
    ) -> dict[uuid.UUID, tuple[int, int]]: ...
    async def message_exists(
        self, tenant_id: TenantId, session_id: SessionId, provider_message_id: str
    ) -> bool: ...
    async def assign_chatbot(
        self,
        tenant_id: TenantId,
        session_ids: list[SessionId],
        chatbot_id: ChatbotId | None,
    ) -> int: ...

    # --- the booking agent's working memory for this thread ------------------
    #
    # Read at the start of every inbound message and written back at the end.
    # An opaque dict here on purpose: the shape belongs to
    # `domain.scheduling.slate`, and the repository's job is to store it, not to
    # understand it. `None` means "this conversation has no booking in flight",
    # which is both the default and what a completed booking resets to.
    async def get_booking_state(
        self, tenant_id: TenantId, session_id: SessionId
    ) -> dict | None: ...
    async def save_booking_state(
        self, tenant_id: TenantId, session_id: SessionId, state: dict | None
    ) -> None: ...


@runtime_checkable
class UsageRepository(Protocol):
    """Per-tenant daily token accounting (replaces Redis at free-tier scale)."""

    async def tokens_used_today(self, tenant_id: TenantId) -> int: ...
    async def add_tokens(self, tenant_id: TenantId, tokens: int) -> None: ...


@dataclass
class ChatbotDailyStat:
    """One day of answer-quality proxies for a chatbot, derived entirely from
    persisted assistant messages (citations + tokens) — no extra instrumentation.
    `avg_top_score` is averaged only over answers that retrieved something, so it
    measures retrieval *strength*; `no_context_rate` measures retrieval *misses*.
    """

    day: date
    answers: int
    avg_top_score: float | None  # None on days where nothing was ever retrieved
    avg_citations: float
    no_context_rate: float  # share of answers with zero retrieved chunks
    refusal_rate: float  # share matching the canonical "not in documents" refusal
    avg_tokens: float


@dataclass
class ProviderStat:
    """Answer counts + quality grouped by the LLM backend that served them — the
    'was a bad day a silent failover?' slice."""

    provider: str | None
    answers: int
    avg_top_score: float | None
    avg_tokens: float


@runtime_checkable
class AnalyticsRepository(Protocol):
    """Read-only aggregate queries over chat history. Tenant-scoped like every
    other repository; never touches the request/answer path."""

    async def chatbot_daily(
        self, tenant_id: TenantId, chatbot_id: ChatbotId, since: datetime
    ) -> list[ChatbotDailyStat]: ...

    async def chatbot_provider_mix(
        self, tenant_id: TenantId, chatbot_id: ChatbotId, since: datetime
    ) -> list[ProviderStat]: ...


@dataclass
class TenantOverview:
    """One row of the Super Admin tenants table — counts and rates only, never
    message content (see `PlatformAdminRepository`'s docstring)."""

    tenant_id: TenantId
    name: str
    slug: str
    is_active: bool
    created_at: datetime
    plan_tier: str
    subscription_status: str
    user_count: int
    assistant_count: int
    tokens_today: int
    tokens_30d: int
    requests_30d: int
    error_rate_30d: float
    refusal_rate_30d: float


@dataclass
class PlatformUser:
    """A row in a tenant's user list, as the Super Admin panel shows it —
    identity and activity metadata only, never password or message content."""

    user_id: UserId
    email: str
    role: str
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime


@dataclass
class TenantDetail:
    overview: TenantOverview
    users: list[PlatformUser]
    usage_daily: list[UsageDay]


@dataclass
class UsageDay:
    day: date
    tokens_used: int


@dataclass
class PlatformHealthDay:
    """One day of the cross-tenant health rollup — the same proxies as
    `ChatbotDailyStat`, summed over every tenant/chatbot instead of one."""

    day: date
    answers: int
    error_rate: float
    refusal_rate: float
    avg_latency_ms: float


@runtime_checkable
class PlatformAdminRepository(Protocol):
    """Cross-tenant reads for the Super Admin panel, and the two status
    toggles that back its "suspend" controls.

    Every query here deliberately omits the `tenant_id` filter every other
    repository in this file applies — that is what makes it cross-tenant. Kept
    to counts, rates, and timestamps: no method here returns `chat_messages`
    or `rag_request_logs.query`/`.answer` content.
    """

    async def tenants_overview(self) -> list[TenantOverview]: ...
    async def tenant_detail(self, tenant_id: TenantId) -> TenantDetail | None: ...
    async def platform_health(self, since: datetime) -> list[PlatformHealthDay]: ...
    async def platform_provider_mix(self, since: datetime) -> list[ProviderStat]: ...


@dataclass
class RequestLog:
    """The per-request evaluation record. One per ask, written success OR failure.
    Deterministic eval fields (no_context, refused, max_score, latency) are filled
    inline; a heavier groundedness score can be attached later by an eval worker."""

    tenant_id: TenantId
    chatbot_id: ChatbotId
    session_id: SessionId
    query: str
    status: str = "ok"  # 'ok' | 'error'
    num_retrieved: int = 0
    max_score: float | None = None
    no_context: bool = False
    refused: bool = False
    answer: str | None = None
    provider: str | None = None
    model: str | None = None
    tokens_used: int = 0
    latency_ms: int = 0
    error: str | None = None
    message_id: MessageId | None = None
    retrieved: list[dict] = field(default_factory=list)
    id: uuid.UUID = field(default_factory=new_id)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@runtime_checkable
class RequestLogRepository(Protocol):
    """Append-only per-request log. Writing must never raise into the answer
    path — a logging failure cannot break a user's chat."""

    async def add(self, log: RequestLog) -> None: ...
    async def list_for_chatbot(
        self, tenant_id: TenantId, chatbot_id: ChatbotId, limit: int = 50
    ) -> list[RequestLog]: ...


@runtime_checkable
class InterviewRepository(Protocol):
    async def add(self, interview: Interview) -> None: ...
    async def get(self, tenant_id: TenantId, interview_id: InterviewId) -> Interview | None: ...
    async def get_by_token(self, access_token: str) -> Interview | None:
        """Resolve by the candidate's opaque access token — NOT tenant-scoped
        (the candidate has no tenant context), mirroring
        ChatbotRepository.get_by_public_key."""
        ...
    async def update(self, interview: Interview) -> None: ...
    async def list_for_tenant(self, tenant_id: TenantId) -> list[Interview]: ...


@runtime_checkable
class InterviewBatchRepository(Protocol):
    async def add(self, batch: InterviewBatch) -> None: ...
    async def get(self, tenant_id: TenantId, batch_id: InterviewBatchId) -> InterviewBatch | None: ...
    async def update(self, batch: InterviewBatch) -> None: ...
    async def list_for_tenant(self, tenant_id: TenantId) -> list[InterviewBatch]: ...
    async def increment_counts(
        self, tenant_id: TenantId, batch_id: InterviewBatchId, *, total: int = 0, sent: int = 0, failed: int = 0
    ) -> None:
        """Atomic SQL-level increment — candidates are processed with bounded
        concurrency, so a read-modify-write here would lose updates."""
        ...


@runtime_checkable
class BatchCandidateRepository(Protocol):
    async def add(self, candidate: BatchCandidate) -> None: ...
    async def add_many(self, candidates: list[BatchCandidate]) -> None: ...
    async def get(
        self, tenant_id: TenantId, candidate_id: BatchCandidateId
    ) -> BatchCandidate | None: ...
    async def update(self, candidate: BatchCandidate) -> None: ...
    async def list_for_batch(self, tenant_id: TenantId, batch_id: InterviewBatchId) -> list[BatchCandidate]: ...


@dataclass
class GoogleOAuthConnection:
    """One tenant's 'Connect Google Calendar' tokens. Absent for a tenant =
    not connected; scheduling an interview simply skips the calendar step."""

    tenant_id: TenantId
    access_token: str
    refresh_token: str
    expires_at: datetime
    scope: str = ""
    connected_email: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@runtime_checkable
class GoogleConnectionRepository(Protocol):
    async def get(self, tenant_id: TenantId) -> GoogleOAuthConnection | None: ...
    async def upsert(self, connection: GoogleOAuthConnection) -> None: ...
    async def delete(self, tenant_id: TenantId) -> None: ...


@dataclass
class OAuthConnection:
    """One tenant's consent to one OAuth provider.

    The generic form of GoogleOAuthConnection above, which is now just this
    record with `provider` pinned to "google_calendar" — kept as its own type so
    interview scheduling, which only ever means the calendar, doesn't have to
    name a provider on every call.
    """

    tenant_id: TenantId
    provider: str
    access_token: str
    expires_at: datetime
    # Empty when the vendor issued none. The connection works until the access
    # token expires, then needs re-consent.
    refresh_token: str = ""
    scope: str = ""
    # "Connected as …" — display only.
    account_label: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@runtime_checkable
class OAuthConnectionRepository(Protocol):
    async def get(self, tenant_id: TenantId, provider: str) -> OAuthConnection | None: ...
    async def list_for_tenant(self, tenant_id: TenantId) -> list[OAuthConnection]: ...
    async def upsert(self, connection: OAuthConnection) -> None: ...
    async def delete(self, tenant_id: TenantId, provider: str) -> None: ...


@dataclass
class WhatsAppChannel:
    """A chatbot deployed to a WhatsApp *business* number. 1:1 with a chatbot —
    one number serves exactly one chatbot. Credentials are per-channel (entered
    by the admin when connecting), not an operator .env concern.

    Two providers reach the same number a customer sees:

      * `twilio` — Twilio's WhatsApp API. Identified by the number itself,
        authenticated per request with an account SID and auth token.
      * `cloud`  — Meta's WhatsApp Cloud API. Identified by an opaque
        `phone_number_id`, authenticated with a long-lived access token, and
        signed with the Meta *app's* secret rather than anything stored here.

    One dataclass with a discriminator rather than two, because everything
    downstream — conversations, the inbox, the broadcast funnel, which assistant
    answers — keys off the channel id and has no reason to care which vendor
    carries the message. Only the send path branches.
    """

    tenant_id: TenantId
    chatbot_id: ChatbotId
    phone_number: str  # E.164, no "whatsapp:" prefix, e.g. "+14155238886"
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    provider: str = "twilio"
    # --- Cloud API only ---
    # Meta's id for the number. The display number in an inbound payload is
    # cosmetic and inconsistently formatted, so this is what a webhook is
    # actually resolved by.
    phone_number_id: str = ""
    # The WhatsApp Business Account the number sits under. Not needed to send —
    # kept because templates and quality ratings are asked of the WABA, and
    # re-finding it by hand later is a support conversation.
    waba_id: str = ""
    access_token: str = ""
    id: uuid.UUID = field(default_factory=new_id)
    status: str = "active"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_cloud(self) -> bool:
        return self.provider == "cloud"


@runtime_checkable
class WhatsAppChannelRepository(Protocol):
    async def add(self, channel: WhatsAppChannel) -> None: ...
    async def get(self, tenant_id: TenantId, channel_id: uuid.UUID) -> WhatsAppChannel | None: ...
    async def get_by_chatbot(
        self, tenant_id: TenantId, chatbot_id: ChatbotId
    ) -> WhatsAppChannel | None: ...
    async def get_by_phone_number(self, phone_number: str) -> WhatsAppChannel | None:
        """Resolve by the Twilio number that received the inbound message —
        NOT tenant-scoped (Twilio's webhook carries no tenant context),
        mirroring ChatbotRepository.get_by_public_key."""
        ...
    async def get_by_phone_number_id(self, phone_number_id: str) -> WhatsAppChannel | None:
        """The Cloud API equivalent: resolve by Meta's `phone_number_id`.

        Also un-scoped, and for the same reason — Meta's webhook knows nothing
        about tenants. This is the whole tenant-resolution step for an inbound
        Cloud message, which is why the column is uniquely indexed."""
        ...
    async def list_for_tenant(self, tenant_id: TenantId) -> list[WhatsAppChannel]: ...
    async def delete(self, tenant_id: TenantId, channel_id: uuid.UUID) -> None: ...


@dataclass
class WhatsAppConversation:
    """Maps one WhatsApp sender to a persistent ChatSession, so a phone
    number gets the same multi-turn memory as the web widget."""

    whatsapp_channel_id: uuid.UUID
    phone_number: str  # the external sender's number
    session_id: SessionId
    # Owning tenant. Added in 0022: the table used to be scoped only through its
    # channel, which the inbox cannot rely on because it queries this table
    # directly. Defaulted so existing constructor calls keep working; the
    # inbound paths set it explicitly.
    tenant_id: TenantId | None = None
    # False for announce-only campaigns: the reply is recorded, not answered.
    # Also what the inbox flips to hand a conversation to a human.
    auto_reply: bool = True
    # --- Denormalized for the thread list (one query, not one per thread) ---
    display_name: str = ""
    last_message_at: datetime | None = None
    last_message_preview: str = ""
    unread_count: int = 0
    has_attachment: bool = False
    # --- Follow-up ladder (see migration 0024) ---
    # When we last spoke and started waiting for an answer. None means we are
    # not waiting on anyone: they replied, or the ladder has been signed off.
    awaiting_reply_since: datetime | None = None
    # Nudges sent since they last said anything. `MAX_FOLLOW_UPS + 1` counts the
    # sign-off, which is what stops the ladder for good.
    followups_sent: int = 0
    # --- Shared-inbox working state (0026) ---
    # Which teammate owns this thread. None = nobody has picked it up, which is
    # a state the inbox filters on rather than an error.
    assignee_id: uuid.UUID | None = None
    assignee_email: str = ""
    tags: list[str] = field(default_factory=list)
    pinned: bool = False
    status: str = "open"
    # --- Contact card (0026) ---
    # Everything WhatsApp does not tell us: learned by whoever is working the
    # thread, and typed into the details panel.
    company: str = ""
    job_title: str = ""
    email: str = ""
    city: str = ""
    country: str = ""
    linkedin_url: str = ""
    source: str = ""
    id: uuid.UUID = field(default_factory=new_id)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def note_message(self, *, preview: str, has_media: bool, inbound: bool) -> None:
        """Fold a newly stored message into the list metadata.

        Unread counts only inbound messages — an assistant answer or an operator
        reply is not something the operator needs to be told about.

        This is also where the follow-up clock starts and stops, because every
        message in either direction passes through here: anything we send means
        we are waiting again, and anything they send means we are not.
        """
        self.last_message_at = datetime.now(UTC)
        self.last_message_preview = (preview or "").strip()[:300]
        if has_media:
            self.has_attachment = True
        if inbound:
            self.unread_count += 1
            # They are back. Stop waiting, and clear the ladder so a future
            # silence is nudged from the top rather than resuming mid-way.
            self.awaiting_reply_since = None
            self.followups_sent = 0
        else:
            self.start_waiting()
        self.updated_at = datetime.now(UTC)

    def start_waiting(self, *, now: datetime | None = None) -> None:
        """Begin (or restart) the countdown to the next follow-up.

        A thread that has already been signed off stays signed off: the
        counter is left alone, so `follow_up_due` keeps refusing it until the
        contact actually replies. Otherwise a manual message months later
        would restart a ladder nobody asked for.
        """
        self.awaiting_reply_since = now or datetime.now(UTC)
        self.updated_at = datetime.now(UTC)

    def follow_up_due(
        self, *, after: timedelta, max_follow_ups: int, now: datetime | None = None
    ) -> bool:
        """Whether this thread has gone quiet long enough to deserve a nudge."""
        if self.awaiting_reply_since is None or not self.auto_reply:
            return False
        # `> max` rather than `>=`: the sign-off is the (max + 1)th message and
        # is still owed to a thread that has had both nudges.
        if self.followups_sent > max_follow_ups:
            return False
        return (now or datetime.now(UTC)) - self.awaiting_reply_since >= after

    def is_final_follow_up(self, *, max_follow_ups: int) -> bool:
        """True when the next thing we send should be the sign-off, not a nudge."""
        return self.followups_sent >= max_follow_ups

    def record_follow_up(self, *, final: bool, now: datetime | None = None) -> None:
        """Book a nudge that has just gone out.

        The sign-off clears `awaiting_reply_since` as well as bumping the
        count: nothing is being waited for any more, which also drops the row
        straight out of the sweep's partial index.
        """
        self.followups_sent += 1
        self.awaiting_reply_since = None if final else (now or datetime.now(UTC))
        self.updated_at = datetime.now(UTC)

    def stop_waiting(self) -> None:
        """Give up on the ladder without sending anything.

        Distinct from `record_follow_up(final=True)`, which books a sign-off
        that a contact actually received. This is for a thread nobody should
        nudge at all — the handset is answered by a different session now, or
        the number it arrived on is gone. Leaving `awaiting_reply_since` set
        would have every sweep, forever, pick the row up, fail, and log.
        """
        self.awaiting_reply_since = None
        self.updated_at = datetime.now(UTC)

    def mark_read(self) -> None:
        self.unread_count = 0
        self.updated_at = datetime.now(UTC)

    def set_auto_reply(self, enabled: bool) -> None:
        self.auto_reply = enabled
        self.updated_at = datetime.now(UTC)


@runtime_checkable
class WhatsAppConversationRepository(Protocol):
    async def get(
        self, whatsapp_channel_id: uuid.UUID, phone_number: str
    ) -> WhatsAppConversation | None: ...
    async def add(self, conversation: WhatsAppConversation) -> None: ...
    async def update(self, conversation: WhatsAppConversation) -> None: ...
    async def get_by_id(
        self, tenant_id: TenantId, conversation_id: uuid.UUID
    ) -> WhatsAppConversation | None: ...
    async def lock_for_follow_up(
        self, tenant_id: TenantId, conversation_id: uuid.UUID
    ) -> WhatsAppConversation | None:
        """The row, locked for the rest of this transaction — or None if
        another transaction already has it locked, meaning this one has
        nothing to do.

        `SendFollowUps` needs this because its own batch read
        (`list_due_follow_ups`, `FOR UPDATE SKIP LOCKED`) commits long before
        anything is sent — that lock cannot, by itself, stop two overlapping
        sweeps from each starting a send for the same contact. Locking again
        here, immediately before sending, is what actually closes that window:
        of two callers racing on the same conversation, one gets the row and
        proceeds, the other gets None and quietly has nothing to do.
        """
        ...
    async def list_for_owner(
        self,
        tenant_id: TenantId,
        owner_id: uuid.UUID,
        *,
        search: str = "",
        has_attachment: bool | None = None,
        unread_only: bool = False,
        auto_reply: bool | None = None,
        limit: int = 30,
        offset: int = 0,
    ) -> list[WhatsAppConversation]: ...
    async def count_for_owner(
        self,
        tenant_id: TenantId,
        owner_id: uuid.UUID,
        *,
        search: str = "",
        has_attachment: bool | None = None,
        unread_only: bool = False,
        auto_reply: bool | None = None,
    ) -> int: ...
    async def list_for_tenant(
        self,
        tenant_id: TenantId,
        *,
        search: str = "",
        has_attachment: bool | None = None,
        unread_only: bool = False,
        auto_reply: bool | None = None,
        limit: int = 30,
        offset: int = 0,
    ) -> list[WhatsAppConversation]: ...
    async def count_for_tenant(
        self,
        tenant_id: TenantId,
        *,
        search: str = "",
        has_attachment: bool | None = None,
        unread_only: bool = False,
        auto_reply: bool | None = None,
    ) -> int: ...
    async def list_due_follow_ups(
        self, *, cutoff: datetime, max_follow_ups: int, limit: int = 50
    ) -> list[WhatsAppConversation]: ...
    async def reassign_owner(
        self, tenant_id: TenantId, from_owner_id: uuid.UUID, to_owner_id: uuid.UUID
    ) -> int:
        """Move every thread on one owner (a channel or a linked number) to
        another, and report how many moved.

        Exists for one job: folding a re-scanned WhatsApp number back into the
        row that already holds its history. A thread whose phone number is
        already present under the target is dropped rather than moved — the
        target's copy is the live one, and the unique (owner, phone) constraint
        would reject the duplicate anyway."""
        ...
    async def stats_for_owners(
        self, tenant_id: TenantId, owner_ids: list[uuid.UUID], *, since: datetime
    ) -> InboxStats:
        """The counters across the top of the inbox, in one round trip."""
        ...
    async def merge_duplicate_threads(
        self,
        tenant_id: TenantId,
        *,
        owner_ids: list[uuid.UUID],
        keep_owner_id: uuid.UUID | None = None,
    ) -> int:
        """Collapse threads that are the same contact into one, and say how many
        were absorbed.

        Two threads are the same contact when their numbers have the same
        digits — which is how one person ended up with three entries in
        Candidates, each holding a different slice of the conversation. The
        loser's messages are re-pointed at the winner's chat session, so this
        merges the history rather than discarding half of it."""
        ...


@dataclass(frozen=True)
class InboxStats:
    """Aggregate counters for the inbox header.

    Computed rather than stored: every figure here is a `count(*)` over rows we
    already keep, and a stored counter is a thing that drifts from the truth the
    first time a write path forgets to bump it.
    """

    conversations: int = 0
    active_conversations: int = 0
    unread: int = 0
    # Outbound in the current window — "messages sent this month".
    messages_sent: int = 0
    messages_received: int = 0
    # Threads the contact has replied on, over threads we have written to. The
    # nearest honest equivalent of a campaign reply rate for an inbox.
    threads_contacted: int = 0
    threads_replied: int = 0


@dataclass
class WhatsAppConversationNote:
    """An internal note on a thread. Never sent to the contact — see the model
    for why it does not live in `chat_messages`."""

    tenant_id: TenantId
    conversation_id: uuid.UUID
    body: str
    author_id: uuid.UUID | None = None
    author_email: str = ""
    id: uuid.UUID = field(default_factory=new_id)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@runtime_checkable
class WhatsAppConversationNoteRepository(Protocol):
    async def add(self, note: WhatsAppConversationNote) -> None: ...
    async def list_for_conversation(
        self, tenant_id: TenantId, conversation_id: uuid.UUID, *, limit: int = 100
    ) -> list[WhatsAppConversationNote]: ...
    async def count_for_conversation(
        self, tenant_id: TenantId, conversation_id: uuid.UUID
    ) -> int: ...
    async def delete(self, tenant_id: TenantId, note_id: uuid.UUID) -> None: ...


@runtime_checkable
class PostCallConfigRepository(Protocol):
    async def add(self, config: PostCallConfig) -> None: ...
    async def get(
        self, tenant_id: TenantId, config_id: uuid.UUID
    ) -> PostCallConfig | None: ...
    async def list_for_chatbot(
        self, tenant_id: TenantId, chatbot_id: ChatbotId
    ) -> list[PostCallConfig]: ...
    async def update(self, config: PostCallConfig) -> None: ...
    async def delete(self, tenant_id: TenantId, config_id: uuid.UUID) -> None: ...


@runtime_checkable
class PostCallDeliveryRepository(Protocol):
    async def claim(self, delivery: PostCallDelivery) -> bool:
        """Reserve (config, session); False if already dispatched. See impl."""
        ...

    async def finish(self, delivery: PostCallDelivery) -> None: ...
    async def list_for_chatbot(
        self, tenant_id: TenantId, chatbot_id: ChatbotId, limit: int = 50
    ) -> list[PostCallDelivery]: ...


@runtime_checkable
class BroadcastRepository(Protocol):
    async def add(self, broadcast: Broadcast) -> None: ...
    async def get(self, tenant_id: TenantId, broadcast_id: uuid.UUID) -> Broadcast | None: ...
    async def get_unscoped(self, broadcast_id: uuid.UUID) -> Broadcast | None: ...
    async def list_for_tenant(self, tenant_id: TenantId) -> list[Broadcast]: ...
    async def list_active(self) -> list[Broadcast]: ...
    async def update(self, broadcast: Broadcast) -> None: ...
    async def delete(self, tenant_id: TenantId, broadcast_id: uuid.UUID) -> None: ...


@runtime_checkable
class BroadcastRecipientRepository(Protocol):
    async def add_many(self, recipients: list[BroadcastRecipient]) -> int: ...
    async def get(
        self, tenant_id: TenantId, recipient_id: uuid.UUID
    ) -> BroadcastRecipient | None: ...
    async def get_by_provider_message_id(
        self, provider_message_id: str
    ) -> BroadcastRecipient | None: ...
    async def get_by_session(self, session_id: SessionId) -> BroadcastRecipient | None: ...
    async def list_for_broadcast(
        self,
        broadcast_id: uuid.UUID,
        *,
        status: str | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[BroadcastRecipient]: ...
    async def count_for_broadcast(
        self, broadcast_id: uuid.UUID, *, status: str | None = None, search: str | None = None
    ) -> int: ...
    async def claim_pending(
        self, broadcast_id: uuid.UUID, limit: int
    ) -> list[BroadcastRecipient]: ...
    async def update(self, recipient: BroadcastRecipient) -> None: ...
    async def reset_failed(self, broadcast_id: uuid.UUID) -> int: ...


@runtime_checkable
class TenantIntegrationRepository(Protocol):
    async def upsert(self, integration: TenantIntegration) -> None: ...
    async def get(
        self, tenant_id: TenantId, integration_id: str
    ) -> TenantIntegration | None: ...
    async def list_for_tenant(self, tenant_id: TenantId) -> list[TenantIntegration]: ...
    async def delete(self, tenant_id: TenantId, integration_id: str) -> None: ...


@runtime_checkable
class IssueReportRepository(Protocol):
    async def add(self, report: IssueReport) -> None: ...
    async def get(self, tenant_id: TenantId, report_id: uuid.UUID) -> IssueReport | None: ...
    async def list_for_tenant(
        self, tenant_id: TenantId, limit: int = 100
    ) -> list[IssueReport]: ...
    async def mark_email_sent(self, report_id: uuid.UUID, sent: bool) -> None: ...


@runtime_checkable
class VoiceProfileRepository(Protocol):
    async def add(self, profile: VoiceProfile) -> None: ...
    async def get(self, tenant_id: TenantId, profile_id: uuid.UUID) -> VoiceProfile | None: ...
    async def list_for_tenant(self, tenant_id: TenantId) -> list[VoiceProfile]: ...
    async def update(self, profile: VoiceProfile) -> None: ...
    async def delete(self, tenant_id: TenantId, profile_id: uuid.UUID) -> None: ...


@runtime_checkable
class WhatsAppWebSessionRepository(Protocol):
    async def add(self, ws: WhatsAppWebSession) -> None: ...
    async def get(
        self, tenant_id: TenantId, session_id: uuid.UUID
    ) -> WhatsAppWebSession | None: ...
    async def get_unscoped(self, session_id: uuid.UUID) -> WhatsAppWebSession | None: ...
    async def list_for_tenant(self, tenant_id: TenantId) -> list[WhatsAppWebSession]: ...
    async def list_linked_to_number(
        self, tenant_id: TenantId, phone_number: str
    ) -> list[WhatsAppWebSession]:
        """Every session in this workspace linked to the same handset.

        Re-scanning the QR for a number that is already connected creates a
        second session, and without this the workspace ends up showing the
        same number twice with its history split between them."""
        ...
    async def list_linked_to_number_anywhere(
        self, phone_number: str
    ) -> list[WhatsAppWebSession]:
        """Every LINKED session on this handset, in any tenant.

        Deliberately unscoped, and the only method on this port that is:
        WhatsApp's multi-device linking has no concept of "this number belongs
        to one workspace" — the same phone can be scanned into two entirely
        different tenants, and both bridge connections then stay genuinely
        alive and both receive every inbound message. `list_linked_to_number`
        catches an operator re-scanning their own number twice; it cannot see
        this, because it never looks past its own tenant. Used only to detect
        and retire a cross-tenant collision the moment a new link completes —
        never for anything that answers a request within a tenant's own scope.
        """
        ...
    async def update(self, ws: WhatsAppWebSession) -> None: ...
    async def delete(self, tenant_id: TenantId, session_id: uuid.UUID) -> None: ...


# --- Scheduling ports -------------------------------------------------------
#
# The seams the appointment use cases depend on. Same rule as everything above:
# every method is tenant-scoped, and the SQLAlchemy implementations live in
# infrastructure/persistence/scheduling_repositories.py.


@dataclass(frozen=True)
class HeldReservation:
    """One row behind a live slot hold.

    A DTO rather than the ORM model: the booking use case needs the resource and
    the reserved window, and handing it a SQLAlchemy object would put persistence
    types in the application layer for the sake of two fields.
    """

    resource_id: ResourceId
    starts_at: datetime
    ends_at: datetime


@runtime_checkable
class LocationRepository(Protocol):
    async def add(self, location: Location) -> None: ...
    async def get(self, tenant_id: TenantId, location_id: LocationId) -> Location | None: ...
    async def list_for_tenant(
        self, tenant_id: TenantId, *, active_only: bool = False
    ) -> list[Location]: ...
    async def update(self, location: Location) -> None: ...


@runtime_checkable
class ServiceRepository(Protocol):
    async def add(self, service: Service) -> None: ...
    async def get(self, tenant_id: TenantId, service_id: ServiceId) -> Service | None: ...
    async def list_for_tenant(
        self, tenant_id: TenantId, *, active_only: bool = False
    ) -> list[Service]: ...
    async def update(self, service: Service) -> None: ...
    async def set_eligibility(
        self, tenant_id: TenantId, service_id: ServiceId, links: list[ServiceResource]
    ) -> None: ...
    async def eligibility_for(
        self, tenant_id: TenantId, service_id: ServiceId
    ) -> list[ServiceResource]: ...


@runtime_checkable
class ResourceRepository(Protocol):
    async def add(self, resource: Resource) -> None: ...
    async def get(self, tenant_id: TenantId, resource_id: ResourceId) -> Resource | None: ...
    async def list_for_tenant(
        self,
        tenant_id: TenantId,
        *,
        location_id: LocationId | None = None,
        kind: str = "",
        active_only: bool = False,
    ) -> list[Resource]: ...
    async def list_by_ids(
        self, tenant_id: TenantId, ids: list[ResourceId]
    ) -> list[Resource]: ...
    async def update(self, resource: Resource) -> None: ...


@runtime_checkable
class AvailabilityRepository(Protocol):
    async def add_rule(self, rule: AvailabilityRule) -> None: ...
    async def rules_for_owners(
        self, tenant_id: TenantId, owner_ids: list[uuid.UUID]
    ) -> dict[object, list[AvailabilityRule]]: ...
    async def list_rules(
        self, tenant_id: TenantId, owner_id: uuid.UUID
    ) -> list[AvailabilityRule]: ...
    async def delete_rule(
        self, tenant_id: TenantId, rule_id: AvailabilityRuleId
    ) -> None: ...
    async def add_block(self, block: BlockedPeriod) -> None: ...
    async def blocks_for_owners(
        self,
        tenant_id: TenantId,
        owner_ids: list[uuid.UUID],
        window_start: datetime,
        window_end: datetime,
    ) -> dict[object, list[BlockedPeriod]]: ...
    async def list_blocks(
        self, tenant_id: TenantId, owner_id: uuid.UUID
    ) -> list[BlockedPeriod]: ...
    async def delete_block(
        self, tenant_id: TenantId, block_id: BlockedPeriodId
    ) -> None: ...


@runtime_checkable
class AppointmentRepository(Protocol):
    async def add(self, appointment: Appointment) -> None: ...
    async def get(
        self, tenant_id: TenantId, appointment_id: AppointmentId
    ) -> Appointment | None: ...
    async def get_by_idempotency_key(
        self, tenant_id: TenantId, key: str
    ) -> Appointment | None: ...
    async def list_for_tenant(
        self,
        tenant_id: TenantId,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        location_id: LocationId | None = None,
        service_id: ServiceId | None = None,
        resource_id: ResourceId | None = None,
        statuses: list[str] | None = None,
        search: str = "",
        # "upcoming" hides anything already finished, "past" shows only those,
        # "" (the default) is everything.
        when: str = "",
        limit: int = 200,
        offset: int = 0,
    ) -> list[Appointment]: ...
    async def list_due_reminders(
        self, *, now: datetime, lead: timedelta, limit: int = 100
    ) -> list[Appointment]:
        """Appointments starting within `lead` that have not been reminded yet.

        Not tenant-scoped — this backs a timer-driven sweep with no principal
        behind it, the same shape as `list_due_follow_ups`."""
        ...
    async def mark_reminder_sent(self, appointment: Appointment) -> None:
        """Record that the reminder went out, so no later sweep repeats it."""
        ...
    async def update(self, appointment: Appointment) -> None: ...
    async def add_status_change(self, change: StatusChange) -> None: ...
    async def history(
        self, tenant_id: TenantId, appointment_id: AppointmentId
    ) -> list[StatusChange]: ...
    async def counts_by_status(
        self, tenant_id: TenantId, window_start: datetime, window_end: datetime
    ) -> dict[str, int]: ...
    async def count_booked_since(
        self, tenant_id: TenantId, since: datetime, *, upcoming_only: bool = True
    ) -> int:
        """Appointments created after `since` — the badge's number.

        Counted on `created_at`, not on when the appointment is *for*: a booking
        taken this morning for next month is news now, and one taken last week
        for tomorrow is not. `upcoming_only` drops bookings that were made for a
        time that has already passed, which are history the moment they land and
        would otherwise sit on the badge unread forever."""
        ...


@runtime_checkable
class ReservationRepository(Protocol):
    """Claimed time. `reserve` raises ConflictError when the slot has gone —
    that is the database's exclusion constraint speaking, not a pre-check."""

    async def busy_intervals(
        self,
        tenant_id: TenantId,
        resource_ids: list[ResourceId],
        window_start: datetime,
        window_end: datetime,
        now: datetime,
    ) -> dict[ResourceId, list[Interval]]: ...
    async def purge_expired_holds(
        self, tenant_id: TenantId, resource_ids: list[ResourceId], now: datetime
    ) -> int: ...
    async def reserve(
        self,
        tenant_id: TenantId,
        resource_ids: list[ResourceId],
        window: Interval,
        *,
        kind: str,
        appointment_id: AppointmentId | None = None,
        hold_token: str | None = None,
        expires_at: datetime | None = None,
    ) -> None: ...
    async def hold_by_token(
        self, tenant_id: TenantId, token: str, now: datetime
    ) -> list[HeldReservation]: ...
    async def convert_hold(
        self, tenant_id: TenantId, token: str, appointment_id: AppointmentId
    ) -> int: ...
    async def release_hold(self, tenant_id: TenantId, token: str, now: datetime) -> int: ...
    async def release_for_appointment(
        self, tenant_id: TenantId, appointment_id: AppointmentId, now: datetime
    ) -> int: ...
    async def sweep_expired(self, now: datetime, limit: int = 500) -> int: ...


class OnboardingRepository(Protocol):
    """Reads the staged shell's two halves.

    `milestones` is a read model over other aggregates' tables — the one place
    allowed to ask "does this tenant have any X at all" across six of them,
    because the alternative is six list calls in the browser. `appointments_ready`
    it leaves to the caller: booking readiness is a computation over hours and
    eligibility, not a row check.
    """

    async def milestones(self, tenant_id: TenantId) -> Milestones: ...
    async def get_prefs(
        self, tenant_id: TenantId, user_id: UserId
    ) -> OnboardingPrefs | None: ...
    async def save_prefs(self, prefs: OnboardingPrefs) -> None: ...


@runtime_checkable
class UnitOfWork(Protocol):
    """Transaction boundary. A use case opens one UoW, does its work through the
    repositories, then commits. Collected domain events are dispatched on commit.
    """

    tenants: TenantRepository
    users: UserRepository
    api_keys: ApiKeyRepository
    subscriptions: SubscriptionRepository
    billing_transactions: BillingTransactionRepository
    api_usage: ApiUsageRepository
    documents: DocumentRepository
    chunks: ChunkRepository
    chatbots: ChatbotRepository
    chats: ChatRepository
    usage: UsageRepository
    analytics: AnalyticsRepository
    request_logs: RequestLogRepository
    interviews: InterviewRepository
    interview_batches: InterviewBatchRepository
    batch_candidates: BatchCandidateRepository
    tenant_invites: TenantInviteRepository
    google_connections: GoogleConnectionRepository
    oauth_connections: OAuthConnectionRepository
    whatsapp_channels: WhatsAppChannelRepository
    whatsapp_conversations: WhatsAppConversationRepository
    whatsapp_conversation_notes: WhatsAppConversationNoteRepository
    post_call_configs: PostCallConfigRepository
    post_call_deliveries: PostCallDeliveryRepository
    broadcasts: BroadcastRepository
    broadcast_recipients: BroadcastRecipientRepository
    tenant_integrations: TenantIntegrationRepository
    issue_reports: IssueReportRepository
    onboarding: OnboardingRepository
    voice_profiles: VoiceProfileRepository
    whatsapp_web_sessions: WhatsAppWebSessionRepository
    # Scheduling (migration 0025).
    locations: LocationRepository
    services: ServiceRepository
    resources: ResourceRepository
    availability: AvailabilityRepository
    appointments: AppointmentRepository
    reservations: ReservationRepository
    platform_admin: PlatformAdminRepository

    async def __aenter__(self) -> UnitOfWork: ...
    async def __aexit__(self, *args: object) -> None: ...
    async def commit(self) -> None: ...
    async def flush(self) -> None: ...
    async def rollback(self) -> None: ...
    def collect_event(self, event: object) -> None: ...
    def set_tenant_scope(self, tenant_id: uuid.UUID | None) -> None:
        """Bind the RLS session variable for this transaction."""
        ...
