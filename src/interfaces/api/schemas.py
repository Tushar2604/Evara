"""HTTP request/response models (the public API contract).

Kept separate from application DTOs so the wire format can evolve independently
of internal use-case signatures.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

# Verdict vocabulary shared with hiring_agent's collect_feedback_tool.
InterviewVerdict = Literal["strong_hire", "hire", "maybe", "no_hire"]
InterviewStatus = Literal["scheduled", "in_progress", "completed", "cancelled"]


# --- Auth ---
class RegisterRequest(BaseModel):
    tenant_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AuthProvidersResponse(BaseModel):
    """Sign-in methods this deployment offers, beyond email + password."""

    google: bool = False


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    is_platform_admin: bool = False


# --- Platform Admin (Super Admin panel: cross-tenant, metadata only) ---
class TenantOverviewResponse(BaseModel):
    tenant_id: uuid.UUID
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


class PlatformUserResponse(BaseModel):
    user_id: uuid.UUID
    email: str
    role: str
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime


class UsageDayResponse(BaseModel):
    day: date
    tokens_used: int


class TenantDetailResponse(BaseModel):
    overview: TenantOverviewResponse
    users: list[PlatformUserResponse]
    usage_daily: list[UsageDayResponse]


class PlatformHealthDayResponse(BaseModel):
    day: date
    answers: int
    error_rate: float
    refusal_rate: float
    avg_latency_ms: float


class PlatformProviderStatResponse(BaseModel):
    provider: str | None
    answers: int
    avg_top_score: float | None
    avg_tokens: float


class PlatformHealthResponse(BaseModel):
    days: int
    daily: list[PlatformHealthDayResponse]
    providers: list[PlatformProviderStatResponse]


class SetStatusRequest(BaseModel):
    is_active: bool


# --- Team (per-tenant admin panel: roles + teammate invites) ---
TeamRole = Literal["admin", "member", "viewer"]


class InviteTeammateRequest(BaseModel):
    email: EmailStr
    role: TeamRole = "member"


class TenantInviteResponse(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    status: str
    expires_at: datetime
    invite_url: str
    email_sent: bool = False


class TeamMemberResponse(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    is_active: bool
    created_at: datetime


class TeamResponse(BaseModel):
    members: list[TeamMemberResponse]
    pending_invites: list[TenantInviteResponse]


class InviteBootstrapResponse(BaseModel):
    tenant_name: str
    email: str
    role: str
    valid: bool


class AcceptInviteRequest(BaseModel):
    password: str = Field(min_length=8, max_length=128)


# --- Documents ---
class CreateUploadRequest(BaseModel):
    filename: str
    content_type: str = "application/octet-stream"
    size_bytes: int = Field(gt=0)


class CreateTextDocumentRequest(BaseModel):
    filename: str = Field(default="", max_length=255)
    text: str = Field(min_length=1, max_length=2_000_000)


class CreateUploadResponse(BaseModel):
    document_id: uuid.UUID
    upload_url: str
    storage_key: str


class DocumentResponse(BaseModel):
    id: uuid.UUID
    filename: str
    status: str
    chunk_count: int
    error: str | None = None


# --- Chatbots ---
class WidgetConfigSchema(BaseModel):
    theme_color: str = Field(default="#4f46e5", max_length=32)
    display_name: str = Field(default="Assistant", min_length=1, max_length=60)
    welcome_message: str = Field(
        default="Hi! 👋 I'm here to help with our open roles — ask me about the positions or start your application.",
        max_length=300,
    )
    launcher_position: Literal["bottom-right", "bottom-left"] = "bottom-right"


class FlowSectionSchema(BaseModel):
    """One block of the Conversational Flow. `id` is absent when the UI has just
    added a section — the server mints one so reorder/toggle can address it."""

    id: uuid.UUID | None = None
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(default="", max_length=6000)
    enabled: bool = True


class AssistantConfigSchema(BaseModel):
    """Runtime settings on the Assistant Details tab — see domain AssistantConfig."""

    direction: Literal["outgoing", "incoming"] = "outgoing"
    languages: list[str] = Field(default_factory=lambda: ["English (India)"], max_length=10)
    # What the assistant writes back in, on text channels — chat, WhatsApp, the
    # widget. Defaults to English rather than mirroring whatever the customer
    # wrote, which used to be the only behaviour and is still available as
    # `RESPONSE_LANGUAGE_AUTO` ("Match the customer's language").
    response_language: str = Field(default="English (India)", max_length=80)
    tts_voice: str = Field(default="Cartesia - Riya", max_length=80)
    llm_model: str = Field(default="gpt-4.1-mini", max_length=80)
    stt_model: str = Field(default="Soniox", max_length=80)
    welcome_message: str = Field(default="", max_length=600)
    welcome_dynamic: bool = True
    welcome_interruptible: bool = False
    # Gives this assistant the appointment tools — it can then check real
    # availability and book, not just answer questions.
    appointments_enabled: bool = False


class AssistantOptionsResponse(BaseModel):
    """The dropdown contents for the Assistant Settings row. Served from the
    domain lists so the UI can never offer a value the backend rejects."""

    languages: list[str]
    # Includes the "match the customer" sentinel as its first entry — the
    # builder renders it as an ordinary option rather than a special case.
    response_languages: list[str]
    tts_voices: list[str]
    llm_models: list[str]
    stt_models: list[str]
    use_cases: list[dict[str, str]]


class GenerateAssistantRequest(BaseModel):
    """Free-text description typed into the create box."""

    description: str = Field(min_length=10, max_length=4000)
    # One of the use-case chips. Free-form rather than an enum so adding a chip
    # is a one-line change; unknown values are simply ignored as a hint.
    use_case: str | None = Field(default=None, max_length=40)
    channel: Literal["text", "voice"] = "voice"


class RegenerateFlowRequest(BaseModel):
    """"Ask AI" on an existing assistant — refine or rebuild its flow."""

    description: str = Field(min_length=10, max_length=4000)
    use_case: str | None = Field(default=None, max_length=40)


class OAuthStartResponse(BaseModel):
    """The vendor consent URL. Returned as JSON rather than a redirect — see
    the module docstring on routers/oauth.py."""

    authorize_url: str


class OAuthStatusResponse(BaseModel):
    provider: str
    connected: bool
    account_label: str = ""
    # False when the server has no OAuth app registered for this vendor, which
    # is a different problem from "you haven't connected yet".
    configured: bool = False


class ChatbotCardCounts(BaseModel):
    """Per-assistant counts the list cards show at a glance."""

    knowledge_files: int = 0
    post_call_actions: int = 0
    integrations: int = 0


class AssistantKnowledgeDocument(BaseModel):
    """One document in this assistant's own knowledge base."""

    id: uuid.UUID
    filename: str
    status: str
    chunk_count: int
    error: str | None = None


class AssistantKnowledgeResponse(BaseModel):
    """This assistant's knowledge base.

    Only its own documents — an assistant never sees, or retrieves from, files
    uploaded for a different one. `ready_count` is what actually answers
    questions; the rest are still ingesting or failed.
    """

    documents: list[AssistantKnowledgeDocument]
    total_count: int
    ready_count: int


class AttachAssistantKnowledgeRequest(BaseModel):
    """Documents just uploaded for this assistant. Additive — see the route."""

    document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)


class CreateChatbotRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    channel: Literal["text", "voice"] = "text"
    system_prompt: str | None = None
    top_k: int = Field(default=5, ge=1, le=20)
    is_public: bool = False
    allowed_document_ids: list[uuid.UUID] = Field(default_factory=list)


class UpdateChatbotRequest(BaseModel):
    """Partial update — only provided fields change. Used by the builder UI to
    edit appearance, sharing, and the embed allowlist."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    channel: Literal["text", "voice"] | None = None
    # `system_prompt` and `flow_sections` are two views of the same thing and
    # are mutually exclusive — sending both is rejected rather than silently
    # letting one win, which would make the editor lose the owner's edits.
    system_prompt: str | None = Field(default=None, min_length=1)
    flow_sections: list[FlowSectionSchema] | None = Field(default=None, max_length=40)
    top_k: int | None = Field(default=None, ge=1, le=20)
    is_public: bool | None = None
    allowed_origins: list[str] | None = Field(default=None, max_length=50)
    widget: WidgetConfigSchema | None = None
    # Cloned voice for spoken replies. Explicit null clears it back to the
    # browser default, so `None` here means "unchanged" and requires the caller
    # to opt in via `voice_profile_id_set`.
    voice_profile_id: uuid.UUID | None = None
    voice_profile_id_set: bool = False
    # Whole-object replace — the settings row is saved as a unit, and a partial
    # merge of eight independent knobs would make "which value won" unanswerable.
    assistant: AssistantConfigSchema | None = None
    # Which documents this assistant may retrieve from. Empty list = every ready
    # document in the tenant (see Chatbot.document_filter).
    allowed_document_ids: list[uuid.UUID] | None = Field(default=None, max_length=500)


class ChatbotResponse(BaseModel):
    id: uuid.UUID
    # Short id for humans ("#236637"). Assigned by the database, so it is
    # absent only for an assistant that has not been persisted yet.
    display_id: int | None = None
    name: str
    channel: Literal["text", "voice"]
    system_prompt: str
    # Empty when this bot was authored as a raw prompt; the editor then offers
    # to convert it into the stock sections.
    flow_sections: list[FlowSectionSchema]
    voice_profile_id: uuid.UUID | None = None
    assistant: AssistantConfigSchema
    top_k: int
    is_public: bool
    public_key: str
    allowed_origins: list[str]
    allowed_document_ids: list[uuid.UUID]
    widget: WidgetConfigSchema
    # Convenience fields the builder UI copies/embeds directly.
    public_url: str
    embed_snippet: str
    # False when the flow came from `fallback_blueprint` rather than the model —
    # only ever set on a generate response, so the UI can label it a draft.
    ai_generated: bool = True
    # Populated on list/get so a card needs no follow-up requests.
    counts: ChatbotCardCounts = Field(default_factory=ChatbotCardCounts)
    # When this assistant was created. Always existed on the row and in the
    # domain entity — it simply never made it onto the wire, so the UI had no
    # way to answer "which of these did I build last week?".
    created_at: datetime


# --- Public widget (no auth; called from third-party pages) ---
class PublicConfigResponse(BaseModel):
    """Bootstrap payload the widget fetches to render itself. Deliberately leaks
    nothing sensitive — just appearance + the bot's display identity."""

    chatbot_id: uuid.UUID
    name: str
    channel: Literal["text", "voice"]
    widget: WidgetConfigSchema
    # What the assistant is configured to speak. Exposed publicly because the
    # widget's microphone has to be set to a language BEFORE anyone talks — a
    # recogniser left on en-US does not transcribe Hindi poorly, it transcribes
    # it as nonsense English. Not sensitive: it is a list of language names,
    # and the visitor is about to hear the answer in one of them anyway.
    languages: list[str] = Field(default_factory=lambda: ["English (India)"])


class PublicCitation(BaseModel):
    ordinal: int
    score: float
    snippet: str


class PublicAnswerResponse(BaseModel):
    answer: str
    citations: list[PublicCitation]


# --- Chat ---
class CreateSessionResponse(BaseModel):
    session_id: uuid.UUID


class AskRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


class CitationResponse(BaseModel):
    document_id: uuid.UUID
    ordinal: int
    score: float
    snippet: str


class AnswerResponse(BaseModel):
    message_id: uuid.UUID
    answer: str
    citations: list[CitationResponse]
    tokens_used: int
    provider: str


# --- Agent ---
class AgentStepResponse(BaseModel):
    index: int
    thought: str
    action: str
    observation: str
    model: str


class AgentAnswerResponse(BaseModel):
    answer: str
    citations: list[CitationResponse]
    tokens_used: int
    provider: str | None
    stop_reason: str  # "final" | "max_steps" | "error"
    tools_used: list[str]
    steps: list[AgentStepResponse]


# --- Analytics ---
class AnalyticsDay(BaseModel):
    day: date
    answers: int
    avg_top_score: float | None
    avg_citations: float
    no_context_rate: float
    refusal_rate: float
    avg_tokens: float


class AnalyticsProvider(BaseModel):
    provider: str | None
    answers: int
    avg_top_score: float | None
    avg_tokens: float


class ChatbotAnalyticsResponse(BaseModel):
    chatbot_id: uuid.UUID
    days: int
    daily: list[AnalyticsDay]
    providers: list[AnalyticsProvider]


class RequestLogResponse(BaseModel):
    id: uuid.UUID
    created_at: datetime
    query: str
    status: str
    num_retrieved: int
    max_score: float | None
    no_context: bool
    refused: bool
    provider: str | None
    model: str | None
    tokens_used: int
    latency_ms: int
    error: str | None
    answer: str | None


# --- Virtual Interview ---
class ScheduleInterviewRequest(BaseModel):
    candidate_name: str = Field(default="", max_length=200)
    candidate_email: EmailStr
    role_title: str = Field(default="", max_length=200)
    job_document_id: uuid.UUID
    resume_document_id: uuid.UUID
    scheduled_at: datetime
    custom_questions: list[str] = Field(default_factory=list)


class QuestionScoreResponse(BaseModel):
    question: str
    answer: str
    score: int
    justification: str


class TranscriptTurnResponse(BaseModel):
    role: Literal["assistant", "user"]
    content: str


class InterviewResponse(BaseModel):
    id: uuid.UUID
    candidate_name: str
    candidate_email: str
    role_title: str
    scheduled_at: datetime
    status: InterviewStatus
    questions: list[str]
    transcript: list[TranscriptTurnResponse]
    calendar_link: str | None
    overall_score: float | None
    overall_verdict: InterviewVerdict | None
    scores: list[QuestionScoreResponse]
    has_report: bool
    join_url: str
    calendar_created: bool = False
    email_sent: bool = False


# --- Virtual Interview: candidate-facing (token-scoped, no auth) ---
class InterviewBootstrapResponse(BaseModel):
    candidate_name: str
    role_title: str
    tenant_name: str
    scheduled_at: datetime
    status: InterviewStatus
    can_join: bool


# --- Bulk interview invites ---
BatchStatus = Literal["collecting", "sending", "completed"]
CandidateStatus = Literal["ingesting", "needs_review", "excluded", "scheduled", "failed"]


class CreateBatchRequest(BaseModel):
    role_title: str = Field(default="", max_length=200)
    job_document_id: uuid.UUID
    window_opens_at: datetime
    window_closes_at: datetime | None = None
    custom_questions: list[str] = Field(default_factory=list)


class AttachBatchResumeRequest(BaseModel):
    resume_document_id: uuid.UUID
    resume_filename: str = Field(default="", max_length=500)


class PatchBatchCandidateRequest(BaseModel):
    candidate_name: str | None = Field(default=None, max_length=200)
    candidate_email: str | None = Field(default=None, max_length=320)
    excluded: bool | None = None


class BatchCandidateResponse(BaseModel):
    id: uuid.UUID
    resume_filename: str
    candidate_name: str
    candidate_email: str
    status: CandidateStatus
    error: str | None
    interview_id: uuid.UUID | None


class InterviewBatchResponse(BaseModel):
    id: uuid.UUID
    role_title: str
    job_document_id: uuid.UUID
    window_opens_at: datetime
    window_closes_at: datetime | None
    custom_questions: list[str] = Field(default_factory=list)
    status: BatchStatus
    total_count: int
    sent_count: int
    failed_count: int
    candidates: list[BatchCandidateResponse] = Field(default_factory=list)


# --- Google Calendar integration ---
class GoogleStatusResponse(BaseModel):
    connected: bool
    email: str = ""


class GoogleConnectResponse(BaseModel):
    authorize_url: str


# --- WhatsApp channel (via Twilio) ---
WhatsAppProviderLiteral = Literal["twilio", "cloud"]


class ConnectWhatsAppRequest(BaseModel):
    """Connect a business number. Which fields are required depends on `provider`.

    The credentials are optional at the schema level and checked in the route
    instead, because "required" is per provider: a Cloud number has no Twilio
    auth token and a Twilio number has no `phone_number_id`. Two request models
    would push that fork into the frontend for no gain.
    """

    chatbot_id: uuid.UUID
    phone_number: str = Field(min_length=5, max_length=32)
    # Defaults to twilio so every existing client keeps working unchanged.
    provider: WhatsAppProviderLiteral = "twilio"
    twilio_account_sid: str = Field(default="", max_length=64)
    twilio_auth_token: str = Field(default="", max_length=255)
    # --- Cloud API ---
    phone_number_id: str = Field(default="", max_length=64)
    waba_id: str = Field(default="", max_length=64)
    # A permanent System User token. Meta's tokens have already grown past 200
    # characters once, so the ceiling is generous.
    access_token: str = Field(default="", max_length=1024)


class WhatsAppChannelResponse(BaseModel):
    id: uuid.UUID
    chatbot_id: uuid.UUID
    chatbot_name: str
    phone_number: str
    provider: WhatsAppProviderLiteral = "twilio"
    status: str
    webhook_url: str
    # Only set for Cloud channels, and only ever the id — the access token is
    # never returned, the same way a password hash never is. Once saved it can
    # be replaced, not read back.
    phone_number_id: str = ""
    # What the operator still has to do in the Meta dashboard, if anything.
    # Empty when the deployment is fully configured.
    setup_warning: str = ""
    created_at: datetime


# --- Post-call delivery ---
CallStatusLiteral = Literal["completed", "voicemail", "no_answer", "busy", "failed"]
DeliveryMethodLiteral = Literal["webhook", "email"]


class PostCallConfigBody(BaseModel):
    """Create/update payload. `webhook_url` and `email_to` are both optional
    here and validated against `delivery_method` in the domain, so the error the
    UI shows is the same one the domain enforces."""

    delivery_method: DeliveryMethodLiteral = "webhook"
    webhook_url: str = Field(default="", max_length=2000)
    email_to: str = Field(default="", max_length=320)
    trigger_statuses: list[CallStatusLiteral] = Field(default_factory=lambda: ["completed"])
    include_summary: bool = True
    include_transcript: bool = True
    include_sentiment: bool = False
    include_extracted: bool = False
    enabled: bool = True


class PostCallConfigResponse(BaseModel):
    id: uuid.UUID
    chatbot_id: uuid.UUID
    delivery_method: DeliveryMethodLiteral
    webhook_url: str
    email_to: str
    trigger_statuses: list[CallStatusLiteral]
    include_summary: bool
    include_transcript: bool
    include_sentiment: bool
    include_extracted: bool
    enabled: bool
    created_at: datetime


class PostCallDeliveryResponse(BaseModel):
    id: uuid.UUID
    config_id: uuid.UUID
    session_id: uuid.UUID
    call_status: CallStatusLiteral
    delivery_method: DeliveryMethodLiteral
    destination: str
    status: str
    error: str
    created_at: datetime


class EndSessionRequest(BaseModel):
    """Closes a conversation and fires any matching post-call configs."""

    call_status: CallStatusLiteral = "completed"


class EndSessionResponse(BaseModel):
    session_id: uuid.UUID
    call_status: CallStatusLiteral
    dispatched: int
    skipped: int


# --- Broadcast campaigns ---
RecipientStatusLiteral = Literal["pending", "sent", "delivered", "read", "replied", "failed"]
BroadcastStatusLiteral = Literal["queued", "sending", "paused", "completed"]


class BroadcastRecipientInput(BaseModel):
    phone_number: str = Field(min_length=5, max_length=32)
    display_name: str = Field(default="", max_length=160)


BroadcastModeLiteral = Literal["broadcast", "broadcast_reply"]
SenderKindLiteral = Literal["cloud_api", "personal"]


class CampaignSenderResponse(BaseModel):
    """One WhatsApp number a campaign can send from.

    Cloud API numbers and QR-linked personal accounts are listed together
    because, from the operator's side, both are just "a WhatsApp number I can
    send from" — the difference only matters to the transport.
    """

    id: uuid.UUID
    kind: SenderKindLiteral
    label: str
    phone_number: str
    # False when the sender exists but can't send right now (a personal account
    # that isn't linked, or a bridge that isn't configured). The reason says why.
    available: bool = True
    unavailable_reason: str = ""
    # The assistant already bound to this number, if any.
    chatbot_id: uuid.UUID | None = None
    chatbot_name: str = ""


class CreateBroadcastRequest(BaseModel):
    chatbot_id: uuid.UUID
    name: str = Field(min_length=1, max_length=160)
    message_template: str = Field(min_length=1, max_length=1600)
    mode: BroadcastModeLiteral = "broadcast_reply"
    # Which number to send from. Omitted = fall back to the assistant's own
    # Cloud API channel, which is how campaigns worked before senders existed.
    sender_kind: SenderKindLiteral | None = None
    sender_id: uuid.UUID | None = None
    # Structured contacts, or a pasted CSV/newline blob — the UI offers both and
    # the server normalizes either into recipients.
    recipients: list[BroadcastRecipientInput] = Field(default_factory=list, max_length=5000)
    recipients_text: str = Field(default="", max_length=500_000)


class AddRecipientsRequest(BaseModel):
    recipients: list[BroadcastRecipientInput] = Field(default_factory=list, max_length=5000)
    recipients_text: str = Field(default="", max_length=500_000)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ForgotPasswordResponse(BaseModel):
    """Always the same shape, whether or not the address exists.

    Reporting "no such account" would turn this endpoint into a way to test
    which email addresses are registered. `email_sent` reflects only whether
    this deployment has email configured at all.
    """

    detail: str
    email_sent: bool


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=10)
    new_password: str = Field(min_length=8, max_length=128)


class PreviewContactsRequest(BaseModel):
    recipients_text: str = Field(default="", max_length=500_000)


class PreviewedContact(BaseModel):
    phone_number: str
    display_name: str = ""


class PreviewContactsResponse(BaseModel):
    """A dry run of the contact parser.

    Exists so the list can be checked before a campaign is created. Previously
    the only way to learn that half a file failed to parse was to create the
    campaign and read the counts afterwards, by which point fixing it means
    editing recipients on a campaign that already exists.
    """

    contacts: list[PreviewedContact]
    # Lines that could not be read as a number, verbatim, so the offending row
    # is recognisable in the original file.
    invalid: list[str]
    duplicates: list[str]
    total_valid: int
    truncated: bool = False


class BroadcastRecipientResponse(BaseModel):
    id: uuid.UUID
    phone_number: str
    display_name: str
    status: RecipientStatusLiteral
    error: str
    session_id: uuid.UUID | None
    attempts: int
    updated_at: datetime


class BroadcastResponse(BaseModel):
    id: uuid.UUID
    chatbot_id: uuid.UUID
    chatbot_name: str
    whatsapp_channel_id: uuid.UUID | None = None
    whatsapp_session_id: uuid.UUID | None = None
    sender_kind: SenderKindLiteral = "cloud_api"
    mode: BroadcastModeLiteral = "broadcast_reply"
    from_number: str
    name: str
    message_template: str
    status: BroadcastStatusLiteral
    total_count: int
    sent_count: int
    delivered_count: int
    read_count: int
    replied_count: int
    failed_count: int
    created_at: datetime
    updated_at: datetime


class RecipientPageResponse(BaseModel):
    """One page of the contacts list, with the funnel counts the filter chips
    render — returned together so the chips can't disagree with the rows."""

    recipients: list[BroadcastRecipientResponse]
    total: int
    page: int
    page_size: int


class AddRecipientsResponse(BaseModel):
    added: int
    duplicates: int
    invalid: list[str]


class BroadcastMessageResponse(BaseModel):
    """One turn of a recipient's conversation, for the Chat Log pane."""

    id: uuid.UUID
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: datetime


class SendManualMessageRequest(BaseModel):
    """Human takeover — send as the assistant, bypassing the RAG pipeline."""

    message: str = Field(min_length=1, max_length=1600)


# --- Integrations catalogue ---
IntegrationCategory = Literal["calendar_crm", "messaging", "data_sheets", "custom_tools"]
IntegrationTiming = Literal["during_call", "post_call"]


class CredentialFieldSchema(BaseModel):
    key: str
    label: str
    placeholder: str = ""
    secret: bool = False
    required: bool = True
    help_text: str = ""


class IntegrationCardResponse(BaseModel):
    """One catalogue card, merged with this tenant's connection state."""

    id: str
    name: str
    description: str
    category: IntegrationCategory
    category_label: str
    timing: IntegrationTiming
    auth: Literal["oauth", "fields"]
    credential_fields: list[CredentialFieldSchema]
    # False = the card renders but Connect is disabled; `unavailable_reason` says why.
    wired: bool
    unavailable_reason: str = ""
    oauth_start_path: str = ""
    connected: bool = False
    enabled: bool = True
    # Secret values are masked — the browser never receives a stored credential.
    config: dict[str, str] = Field(default_factory=dict)
    connected_at: datetime | None = None


class IntegrationCatalogueResponse(BaseModel):
    integrations: list[IntegrationCardResponse]
    # Chip counts, keyed by category plus "all".
    counts: dict[str, int]
    connected_count: int


class ConnectIntegrationRequest(BaseModel):
    # Free-form because each integration declares its own fields; the server
    # keeps only keys the spec names and drops the rest.
    config: dict[str, str] = Field(default_factory=dict)


class IntegrationTestResponse(BaseModel):
    ok: bool
    message: str


# --- Report an issue ---
ReportTypeLiteral = Literal["bug", "feature_request", "question", "billing", "other"]
PriorityLiteral = Literal["low", "medium", "high", "critical"]
IssueStatusLiteral = Literal["open", "in_progress", "resolved", "closed"]


class CreateIssueReportRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    email: EmailStr
    phone: str = Field(default="", max_length=32)
    report_type: ReportTypeLiteral
    priority: PriorityLiteral = "medium"
    subject: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=5000)
    page_url: str = Field(default="", max_length=500)


class IssueReportResponse(BaseModel):
    id: uuid.UUID
    name: str
    email: str
    phone: str
    report_type: ReportTypeLiteral
    priority: PriorityLiteral
    subject: str
    description: str
    status: IssueStatusLiteral
    page_url: str
    email_sent: bool
    created_at: datetime


class IssueOptionsResponse(BaseModel):
    """Drives the form's dropdowns so the labels live in one place."""

    report_types: list[dict[str, str]]
    priorities: list[dict[str, str]]
    support_email_configured: bool


# --- Cloned voices ---
VoiceGenderLiteral = Literal["female", "male", "neutral"]
VoiceStatusLiteral = Literal["pending", "ready", "failed"]


class VoiceProfileResponse(BaseModel):
    id: uuid.UUID
    name: str
    gender: VoiceGenderLiteral
    language: str
    description: str
    duration_seconds: float
    sample_bytes: int
    provider: str
    status: VoiceStatusLiteral
    error: str
    created_at: datetime


class VoiceOptionsResponse(BaseModel):
    languages: list[dict[str, str]]
    genders: list[dict[str, str]]
    min_seconds: int
    max_seconds: int
    max_mb: int
    # False = samples are still recorded, stored, and listed, but cloning is
    # unavailable. The UI says so rather than failing at submit time.
    cloning_enabled: bool
    provider: str


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


# --- Personal WhatsApp (QR / multi-device) ---
WhatsAppWebStatusLiteral = Literal[
    "pending", "awaiting_scan", "linked", "disconnected", "logged_out", "failed"
]


class WhatsAppWebSessionResponse(BaseModel):
    id: uuid.UUID
    chatbot_id: uuid.UUID | None
    chatbot_name: str = ""
    status: WhatsAppWebStatusLiteral
    phone_number: str
    display_name: str
    # Only populated while a scan is pending and the code is still valid.
    qr_data_url: str = ""
    qr_seconds_remaining: int = 0
    last_error: str = ""
    health: str
    linked_at: datetime | None = None
    created_at: datetime


class WhatsAppWebOptionsResponse(BaseModel):
    """Whether the bridge is configured and reachable, so the Channels page can
    disable the method instead of failing at scan time."""

    enabled: bool
    bridge_healthy: bool
    message: str = ""


class AttachAssistantRequest(BaseModel):
    # Null detaches: messages keep arriving and are stored, nothing replies.
    chatbot_id: uuid.UUID | None = None


class BridgeEventRequest(BaseModel):
    """Posted by the Node bridge. Authenticated by a shared secret header, not
    a JWT — the bridge acts for no particular user."""

    session_id: uuid.UUID
    event: Literal["qr", "linked", "disconnected", "logged_out", "failed", "message"]
    qr_data_url: str = ""
    phone_number: str = ""
    display_name: str = ""
    error: str = ""
    # message events. `from` is a Python keyword, so it arrives via an alias.
    from_: str = Field(default="", alias="from")
    jid: str = ""
    text: str = ""
    message_id: str = ""
    pushname: str = ""
    # "in" from the contact, "out" for a message the operator typed on the phone
    # itself. Outbound messages are recorded so the inbox matches WhatsApp, but
    # never answered — that is how the assistant avoids replying to itself.
    direction: Literal["in", "out"] = "in"
    # What the thread list shows: the caption, or a label like "Photo".
    preview: str = ""
    # True when WhatsApp flushed this message on reconnect rather than
    # delivering it on a live socket. A *hint*, not a veto: Baileys sets it for
    # anything queued while the socket was down, which includes a campaign
    # reply that arrived seconds ago. Answerability is decided from
    # `timestamp`; genuine history backfill never reaches this endpoint (it
    # goes to /bridge-history).
    synced: bool = False
    # Unix seconds when WhatsApp stamped the message. 0 when the bridge did not
    # report one, which is read as "age unknown" and does not block a reply.
    timestamp: int = 0
    # Attachment metadata. `media_storage_key` is set once the bridge has
    # uploaded the bytes; `media_error` explains an attachment we know arrived
    # but could not store (too large, download failed).
    media_kind: str = ""
    media_mime_type: str = ""
    media_filename: str = ""
    media_size_bytes: int = 0
    media_storage_key: str = ""
    media_error: str = ""

    model_config = {"populate_by_name": True}


class BridgeMediaResponse(BaseModel):
    storage_key: str


class BridgeHistoryContact(BaseModel):
    phone: str
    name: str = ""


class BridgeHistoryMessage(BaseModel):
    phone: str
    text: str = ""
    # The message's real time on WhatsApp. Without it an import would stamp a
    # whole archive with the same instant and destroy thread ordering.
    timestamp: datetime | None = None
    direction: Literal["in", "out"] = "in"
    message_id: str = ""
    pushname: str = ""
    preview: str = ""
    media_kind: str = ""
    media_mime_type: str = ""
    media_filename: str = ""
    media_size_bytes: int = 0


class BridgeHistoryRequest(BaseModel):
    """One chunk of the history WhatsApp pushes after a device links. Contacts
    and messages arrive in separate batches, so both lists are optional."""

    session_id: uuid.UUID
    contacts: list[BridgeHistoryContact] = Field(default_factory=list)
    messages: list[BridgeHistoryMessage] = Field(default_factory=list)


class BridgeHistoryResponse(BaseModel):
    contacts_imported: int
    messages_imported: int
    skipped_duplicates: int


# --- WhatsApp inbox ---


class InboxConversationResponse(BaseModel):
    """One thread in the inbox list. Everything here is denormalized onto
    `whatsapp_conversations`, so the list renders without a message query per
    row."""

    id: uuid.UUID
    phone_number: str
    display_name: str
    last_message_at: datetime | None
    last_message_preview: str
    unread_count: int
    has_attachment: bool
    # False means a human has taken the conversation over and the assistant is
    # staying quiet.
    auto_reply: bool
    # --- Shared-inbox working state ---
    assignee_id: uuid.UUID | None = None
    # Resolved server-side: a teammate's id is not something the browser can
    # render, and asking it to fetch the team to translate one is a round trip
    # per list.
    assignee_email: str = ""
    tags: list[str] = Field(default_factory=list)
    pinned: bool = False
    status: Literal["open", "closed"] = "open"
    # --- Contact card ---
    company: str = ""
    job_title: str = ""
    email: str = ""
    city: str = ""
    country: str = ""
    linkedin_url: str = ""
    source: str = ""


class InboxConversationPageResponse(BaseModel):
    conversations: list[InboxConversationResponse]
    total: int
    page: int
    page_size: int


class InboxMessageResponse(BaseModel):
    id: uuid.UUID
    # "in" from the contact, "out" from the assistant or a human operator.
    direction: Literal["in", "out"]
    # Who wrote an outgoing message. "contact" for inbound. Derived from the
    # message's `provider`, which already distinguished these three but was
    # never exposed — so the inbox could not show whether the assistant had
    # actually answered anything, which is the one thing you want to see after
    # attaching an agent to a number.
    author: Literal["contact", "assistant", "operator", "device"] = "contact"
    content: str
    created_at: datetime
    media_kind: str = ""
    media_mime_type: str = ""
    media_filename: str = ""
    media_size_bytes: int = 0
    # True when the attachment's bytes are retrievable; false when WhatsApp sent
    # one but it could not be stored, so the UI can say so instead of offering a
    # download that will fail.
    media_available: bool = False


class InboxSendRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4096)


class InboxConversationUpdate(BaseModel):
    """Every field optional, and each edit arrives on its own.

    The inbox marks a thread read on open, toggles takeover from the header,
    assigns from a menu and saves the contact card from a panel — four
    independent gestures. A partial patch is what lets each of them send only
    what it changed, so two people working the same thread cannot clobber each
    other's unrelated edits.

    `assignee_id` is the one field where "not sent" and "sent as null" must
    differ — null means *unassign* — so it carries an explicit sentinel rather
    than relying on None.
    """

    auto_reply: bool | None = None
    mark_read: bool | None = None
    assignee_id: uuid.UUID | None = None
    # Set true alongside a null `assignee_id` to actually clear the owner.
    unassign: bool = False
    tags: list[str] | None = Field(default=None, max_length=12)
    pinned: bool | None = None
    status: Literal["open", "closed"] | None = None
    company: str | None = Field(default=None, max_length=160)
    job_title: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=254)
    city: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, max_length=120)
    linkedin_url: str | None = Field(default=None, max_length=300)
    source: str | None = Field(default=None, max_length=60)
    display_name: str | None = Field(default=None, max_length=160)


class InboxNoteRequest(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class InboxNoteResponse(BaseModel):
    id: uuid.UUID
    body: str
    author_email: str = ""
    created_at: datetime


class InboxStatsResponse(BaseModel):
    """The counters across the top of the inbox.

    Rates are served already computed: the denominator rules ("of threads we
    wrote to, how many replied") belong in one place, and re-deriving them in
    the browser is how two surfaces end up disagreeing about the same number.
    """

    connected_numbers: int = 0
    conversations: int = 0
    active_conversations: int = 0
    unread: int = 0
    messages_sent: int = 0
    messages_received: int = 0
    # 0-100, one decimal.
    delivery_rate: float = 0.0
    read_rate: float = 0.0
    reply_rate: float = 0.0
    active_campaigns: int = 0
    # What window `messages_sent` covers, so the label can say so honestly.
    period_label: str = ""


class BookingReadinessResponse(BaseModel):
    """Whether a booking assistant can actually book anything yet.

    Four things have to exist before `find_available_slots` can return a single
    time: a location, a service, a staff member or room assigned to that
    service, and opening hours for them. Miss any one and the search returns
    nothing — which the assistant correctly reports as "no times available",
    and which looks exactly like a broken assistant from the outside.

    So the check is served rather than described. `blockers` is ordered in the
    sequence they have to be fixed, because a service cannot be assigned staff
    that do not exist yet.
    """

    ready: bool = False
    locations: int = 0
    services: int = 0
    resources: int = 0
    # Services with at least one staff member or room attached. A service with
    # none is bookable in theory and never in practice.
    services_with_staff: int = 0
    # Resources with at least one opening-hours rule. Informational: a resource
    # without its own hours inherits the branch's, which is the usual setup.
    resources_with_hours: int = 0
    # Locations with opening hours. This is the one that decides whether any
    # slot can exist at all — a branch with no hours is closed.
    locations_with_hours: int = 0
    blockers: list[str] = Field(default_factory=list)


class NewAppointmentsResponse(BaseModel):
    """How many bookings have landed since the caller last looked.

    `since` is echoed back so the client can tell a real zero from a request it
    sent with the wrong watermark — the badge is only trustworthy if both ends
    agree on what "new" is being measured from.
    """

    count: int = 0
    since: datetime
    # The newest booking's timestamp, or None when nothing is new. The client
    # advances its watermark to this rather than to "now", so a booking that
    # lands between the query and the click is not silently marked as seen.
    latest_at: datetime | None = None


class ReplyCheck(BaseModel):
    name: str
    ok: bool
    detail: str = ""


class ReplyReadinessResponse(BaseModel):
    """Why this number is or is not answering, right now.

    There are several independent conditions between an inbound WhatsApp
    message and a generated reply, and every one of them fails *silently* —
    each writes a different line to the server log and nothing to the screen.
    That is why "the assistant stopped replying" kept coming back looking like
    the same bug: it was a different gate each time, and there was no way to
    tell which without reading logs.

    This checks them in the order they actually fire, and names the first one
    that is closed.
    """

    ready: bool = False
    # A short machine-readable cause, so the UI can choose its own wording.
    # "" when ready.
    reason: str = ""
    # What to do about it, in the operator's language.
    detail: str = ""
    # Every check, in order, for the cases where more than one is wrong.
    checks: list[ReplyCheck] = Field(default_factory=list)


class MergeDuplicateNumbersResponse(BaseModel):
    """Result of folding duplicates back together.

    Two counters because there are two kinds: a number connected twice
    (`merged_sessions`), and one contact written two ways under a single number
    (`merged_threads`). Both present as "the same number in three places".
    """

    merged_sessions: int = 0
    moved_conversations: int = 0
    merged_threads: int = 0


# --- Candidates (tenant-wide, read-oriented view over every WhatsApp number) ---


class CandidateResponse(BaseModel):
    """One WhatsApp contact, labelled with whichever number the conversation
    landed on. Same underlying row as `InboxConversationResponse` — this is
    the tenant-wide version, not scoped to one number."""

    id: uuid.UUID
    phone_number: str
    display_name: str
    last_message_at: datetime | None
    last_message_preview: str
    unread_count: int
    has_attachment: bool
    auto_reply: bool
    channel_kind: SenderKindLiteral
    channel_label: str
    # Only set for a "personal" (QR-linked) number — the only kind with a live
    # reply inbox today — so the frontend knows when it can offer a deep link
    # to keep replying rather than pretending every candidate has one.
    session_id: uuid.UUID | None = None
    # Shown on the card without opening the thread: "have they actually talked
    # to us, and did they send anything?" is most of what the grid is scanned
    # for. Counted in one grouped query per page, not per card.
    message_count: int = 0
    document_count: int = 0
    # Where this contact sits in the follow-up ladder, so a card can say
    # "chased twice, gone quiet" rather than looking identical to a live one.
    followups_sent: int = 0
    awaiting_reply: bool = False
    # Every thread this person has, across every connected number — this card
    # represents the most recently active one. Usually a single entry; more
    # than one means they have talked to the workspace on two numbers, which is
    # two real conversations to switch between, not a duplicate to delete.
    threads: list[CandidateThreadResponse] = Field(default_factory=list)


class CandidateThreadResponse(BaseModel):
    """One of a contact's conversations, labelled with the number it is on."""

    conversation_id: uuid.UUID
    # The connected number that owns it: a linked personal session, or a Cloud
    # API channel. Null when that number has since been disconnected.
    session_id: uuid.UUID | None = None
    channel_kind: SenderKindLiteral = "personal"
    channel_label: str = ""
    last_message_at: datetime | None = None
    message_count: int = 0
    unread_count: int = 0


class ConnectedNumberResponse(BaseModel):
    """A number the Candidates page can filter by.

    Both kinds in one list — an operator picking "which WhatsApp number" does
    not care whether it is a linked handset or a Cloud API sender, and making
    them choose a tab first would be asking about our implementation.
    """

    id: uuid.UUID
    kind: SenderKindLiteral
    phone_number: str
    label: str
    # Personal numbers only: whether the socket is currently up.
    connected: bool = True
    contact_count: int = 0


class CandidatePageResponse(BaseModel):
    candidates: list[CandidateResponse]
    total: int
    page: int
    page_size: int


class TranscriptionStatusResponse(BaseModel):
    """Whether server dictation is available, so the mic button can choose its
    path before the user presses it rather than after."""

    enabled: bool
    provider: str = ""
    model: str = ""
    max_seconds: int = 120


class TranscriptionResponse(BaseModel):
    text: str
    provider: str = ""


class CrmDestinationResponse(BaseModel):
    """Where "Send to CRM" would send, so the UI can offer the action, or say
    what is missing, without first firing a request that would 400."""

    connected: bool
    # Host only, never the full URL: the path of a catch-hook URL is its
    # credential, and this response is read by anyone who can see a candidate.
    endpoint_host: str = ""
    # Where an admin goes to set it up. Constant, but returned so the message
    # and the link stay together.
    settings_path: str = "/integrations"


class CrmExportResponse(BaseModel):
    delivered: bool
    message: str
    endpoint_host: str = ""


# --- Scheduling / Appointments ----------------------------------------------
#
# The wire contract for the appointment engine. Statuses and sources are Literals
# rather than plain strings so an unknown value is a 422 at the boundary instead
# of a row the calendar cannot render.

AppointmentStatusLiteral = Literal[
    "draft",
    "requested",
    "pending",
    "awaiting_confirmation",
    "confirmed",
    "arrived",
    "checked_in",
    "in_progress",
    "completed",
    "cancelled",
    "no_show",
    "rescheduled",
    "waitlisted",
]
BookingSourceLiteral = Literal[
    "staff",
    "ai_voice",
    "whatsapp",
    "web_widget",
    "booking_page",
    "sms",
    "email",
    "mobile_app",
    "portal",
    "api",
    "campaign",
]
ResourceKindLiteral = Literal["staff", "room", "equipment", "vehicle", "other"]
OwnerKindLiteral = Literal["location", "resource"]


class LocationRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    # IANA name. Validated against the tz database in the domain, so a typo is a
    # 422 here rather than an exception on the first availability query.
    timezone: str = Field(default="UTC", max_length=64)
    address: str = Field(default="", max_length=2000)
    phone: str = Field(default="", max_length=32)
    email: str = Field(default="", max_length=320)
    is_active: bool = True


class LocationResponse(BaseModel):
    id: uuid.UUID
    name: str
    timezone: str
    address: str = ""
    phone: str = ""
    email: str = ""
    is_active: bool = True
    created_at: datetime


class ServiceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    duration_minutes: int = Field(ge=1, le=1440)
    category: str = Field(default="", max_length=160)
    description: str = Field(default="", max_length=4000)
    buffer_before_minutes: int = Field(default=0, ge=0, le=480)
    buffer_after_minutes: int = Field(default=0, ge=0, le=480)
    # Minor units, never a float — a rounded price is a support ticket.
    price_cents: int = Field(default=0, ge=0)
    deposit_cents: int = Field(default=0, ge=0)
    currency: str = Field(default="AED", max_length=3)
    min_notice_minutes: int = Field(default=0, ge=0)
    max_horizon_days: int = Field(default=60, ge=1, le=730)
    cancellation_window_hours: int = Field(default=0, ge=0)
    online_bookable: bool = True
    is_active: bool = True


class ServiceResourceLink(BaseModel):
    """One eligible resource, in one role.

    The role is what lets a service require a practitioner AND a room: two links
    with different roles, both of which must be fillable for a slot to exist.
    """

    resource_id: uuid.UUID
    role: str = Field(default="primary", max_length=40)
    required: bool = True


class ServiceResponse(BaseModel):
    id: uuid.UUID
    name: str
    category: str = ""
    description: str = ""
    duration_minutes: int
    buffer_before_minutes: int = 0
    buffer_after_minutes: int = 0
    price_cents: int = 0
    deposit_cents: int = 0
    currency: str = "AED"
    min_notice_minutes: int = 0
    max_horizon_days: int = 60
    cancellation_window_hours: int = 0
    online_bookable: bool = True
    is_active: bool = True
    resources: list[ServiceResourceLink] = Field(default_factory=list)
    created_at: datetime


class SetServiceResourcesRequest(BaseModel):
    """The complete intended eligibility for a service.

    Replaces rather than merges — the editor always sends the full set, and
    diffing here would silently keep a resource the user removed.
    """

    resources: list[ServiceResourceLink] = Field(default_factory=list)


class ResourceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    kind: ResourceKindLiteral = "staff"
    location_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    email: str = Field(default="", max_length=320)
    phone: str = Field(default="", max_length=32)
    capacity: int = Field(default=1, ge=1, le=1000)
    timezone: str = Field(default="", max_length=64)
    color: str = Field(default="", max_length=16)
    is_active: bool = True


class ResourceResponse(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    location_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    email: str = ""
    phone: str = ""
    capacity: int = 1
    timezone: str = ""
    color: str = ""
    is_active: bool = True
    created_at: datetime


class AvailabilityRuleRequest(BaseModel):
    owner_kind: OwnerKindLiteral
    owner_id: uuid.UUID
    # Monday = 0, matching `datetime.weekday()`.
    weekday: int = Field(ge=0, le=6)
    # Wall-clock local time ("09:00"), NOT an instant: "Mondays 09:00" must stay
    # 09:00 across a daylight-saving change.
    start_time: time
    end_time: time
    effective_from: datetime | None = None
    effective_until: datetime | None = None


class AvailabilityRuleResponse(BaseModel):
    id: uuid.UUID
    owner_kind: str
    owner_id: uuid.UUID
    weekday: int
    start_time: time
    end_time: time
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    is_active: bool = True


class BlockedPeriodRequest(BaseModel):
    owner_kind: OwnerKindLiteral
    owner_id: uuid.UUID
    starts_at: datetime
    ends_at: datetime
    reason: str = Field(default="", max_length=300)


class BlockedPeriodResponse(BaseModel):
    id: uuid.UUID
    owner_kind: str
    owner_id: uuid.UUID
    starts_at: datetime
    ends_at: datetime
    reason: str = ""


class SlotResponse(BaseModel):
    """One bookable start time.

    `resource_ids` is part of the contract, not decoration: booking this slot
    reserves exactly these resources, which is what makes the booking call a
    confirmation of this offer rather than a second, racing search.
    """

    starts_at: datetime
    ends_at: datetime
    resource_ids: list[uuid.UUID]


class AvailabilityResponse(BaseModel):
    location_id: uuid.UUID
    service_id: uuid.UUID
    # The branch's zone, so a client can render these instants in local time
    # without a second lookup.
    timezone: str
    duration_minutes: int
    slots: list[SlotResponse]


class SlotHoldRequest(BaseModel):
    location_id: uuid.UUID
    service_id: uuid.UUID
    starts_at: datetime
    resource_id: uuid.UUID | None = None


class SlotHoldResponse(BaseModel):
    token: str
    starts_at: datetime
    ends_at: datetime
    expires_at: datetime
    resource_ids: list[uuid.UUID]


class CreateAppointmentRequest(BaseModel):
    location_id: uuid.UUID
    service_id: uuid.UUID
    starts_at: datetime
    customer_name: str = Field(min_length=1, max_length=160)
    customer_phone: str = Field(default="", max_length=32)
    customer_email: str = Field(default="", max_length=320)
    customer_timezone: str = Field(default="", max_length=64)
    resource_id: uuid.UUID | None = None
    # Present when the caller already held the slot. The hold is converted in
    # place, so the slot is never free for an instant in between.
    hold_token: str = Field(default="", max_length=64)
    source: BookingSourceLiteral = "staff"
    status: AppointmentStatusLiteral = "pending"
    customer_notes: str = Field(default="", max_length=4000)
    internal_notes: str = Field(default="", max_length=4000)
    # Supplied by the caller so a retried POST returns the booking it already
    # made instead of creating a second one.
    idempotency_key: str = Field(default="", max_length=128)


class UpdateAppointmentRequest(BaseModel):
    """Details only. Moving an appointment in time goes through /reschedule, so
    a general PATCH can never reach the reservation logic by accident."""

    customer_name: str | None = Field(default=None, max_length=160)
    customer_phone: str | None = Field(default=None, max_length=32)
    customer_email: str | None = Field(default=None, max_length=320)
    customer_timezone: str | None = Field(default=None, max_length=64)
    customer_notes: str | None = Field(default=None, max_length=4000)
    internal_notes: str | None = Field(default=None, max_length=4000)


class RescheduleAppointmentRequest(BaseModel):
    starts_at: datetime
    resource_id: uuid.UUID | None = None
    reason: str = Field(default="", max_length=500)


class AppointmentActionRequest(BaseModel):
    """Body for confirm / cancel / check-in / complete / no-show."""

    reason: str = Field(default="", max_length=500)


class AppointmentResponse(BaseModel):
    id: uuid.UUID
    location_id: uuid.UUID
    service_id: uuid.UUID
    starts_at: datetime
    ends_at: datetime
    timezone: str
    status: str
    source: str
    customer_name: str
    customer_phone: str = ""
    customer_email: str = ""
    customer_timezone: str = ""
    resource_ids: list[uuid.UUID] = Field(default_factory=list)
    customer_notes: str = ""
    internal_notes: str = ""
    cancellation_reason: str = ""
    rescheduled_from_id: uuid.UUID | None = None
    # Denormalized labels so a calendar render needs one request, not four.
    location_name: str = ""
    service_name: str = ""
    resource_names: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class AppointmentPageResponse(BaseModel):
    appointments: list[AppointmentResponse]
    total: int
    page: int
    page_size: int


class AppointmentHistoryEntry(BaseModel):
    """One line of the audit trail (spec section 40)."""

    from_status: str
    to_status: str
    actor_kind: str
    actor_label: str = ""
    channel: str = ""
    reason: str = ""
    occurred_at: datetime


class AppointmentHistoryResponse(BaseModel):
    appointment_id: uuid.UUID
    entries: list[AppointmentHistoryEntry]


class AppointmentSummaryResponse(BaseModel):
    """Status tallies for the dashboard, over one window."""

    window_start: datetime
    window_end: datetime
    total: int
    by_status: dict[str, int]


# --- Onboarding / staged navigation -----------------------------------------


class MilestonesSchema(BaseModel):
    assistant_configured: bool
    knowledge_ready: bool
    assistant_tested: bool
    channel_connected: bool
    assistant_live: bool
    appointments_ready: bool
    integrations_connected: bool


class NextStepSchema(BaseModel):
    key: str
    title: str
    body: str
    href: str
    cta: str
    tour: str | None = None


class OnboardingStateResponse(BaseModel):
    """Everything the shell needs to decide what to reveal, in one request.

    Replaces six list calls the dashboard used to make purely to count rows.
    `stage` is derived from `milestones` server-side so the frontend can never
    hold a different opinion about it than the backend does.
    """

    stage: str
    milestones: MilestonesSchema
    next_step: NextStepSchema | None = None
    nav_mode: str = "guided"
    tours_completed: list[str] = Field(default_factory=list)
    dismissed: list[str] = Field(default_factory=list)
    celebrated_stages: list[str] = Field(default_factory=list)


class OnboardingPrefsUpdate(BaseModel):
    """A partial update. Every field is optional and every list field appends
    rather than replaces — two tabs open on the same account must not be able
    to undo each other's dismissals by racing."""

    nav_mode: Literal["guided", "full"] | None = None
    complete_tour: str | None = None
    dismiss: str | None = None
    celebrate_stage: str | None = None
    # The one destructive option, and the only way back from "I dismissed
    # everything": clears dismissals and tour history so the guidance returns.
    reset: bool = False


# --- Billing & API access ---
# The wire shape of the plan catalogue and what a workspace has used against
# it. `max_assistants: null` and `assistants_remaining: null` both mean
# unlimited — the headline of every paid tier — and the frontend renders them
# as the word, not as a missing number.
PlanTierName = Literal["free", "starter", "growth", "scale"]


class PlanSchema(BaseModel):
    tier: PlanTierName
    name: str
    price_usd: float
    tagline: str
    max_assistants: int | None
    max_documents: int
    daily_token_quota: int
    monthly_api_calls: int
    scopes: list[str]
    api_access: bool
    agent_api: bool


class PlanUsageSchema(BaseModel):
    assistants_used: int
    assistants_remaining: int | None
    documents_used: int
    tokens_used_today: int
    api_calls_this_period: int
    api_calls_remaining: int


class SubscriptionSchema(BaseModel):
    tier: PlanTierName
    status: str
    started_at: datetime
    current_period_end: datetime | None
    auto_renew: bool
    canceled_at: datetime | None


class BillingOverviewResponse(BaseModel):
    """One request behind the whole Billing page: what they're on, what it
    grants, what they've used, and the catalogue to compare against."""

    subscription: SubscriptionSchema
    plan: PlanSchema
    usage: PlanUsageSchema
    available_plans: list[PlanSchema]


class ChangePlanRequest(BaseModel):
    tier: PlanTierName
    # The payment-processor charge id, once one exists. Accepted now so the
    # billing history has somewhere to put it without a schema change.
    reference: str | None = None


class BillingTransactionResponse(BaseModel):
    id: uuid.UUID
    kind: str
    amount_usd: float
    description: str
    plan_tier: str | None
    reference: str | None
    created_at: datetime


class CreateApiKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    # Omitted = every scope the plan grants, which is what "Create key" means.
    # Naming scopes explicitly is for callers who want a narrower key.
    scopes: list[str] | None = None


class ApiKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    # The first characters of the key, so a row is identifiable without the
    # secret ever being retrievable again.
    prefix: str
    scopes: list[str]
    # What the key can do *today* — its scopes intersected with the live plan.
    # Differs from `scopes` after a downgrade, and that difference is the point.
    active_scopes: list[str]
    plan_tier: str
    is_active: bool
    last_used_at: datetime | None
    created_at: datetime


class CreatedApiKeyResponse(BaseModel):
    """The one and only response that carries the raw key. It is not stored in
    a form we can read back, so this is the customer's single chance to copy
    it — the UI says so, loudly."""

    key: ApiKeyResponse
    raw_key: str


class ApiKeyListResponse(BaseModel):
    keys: list[ApiKeyResponse]
    plan_tier: str
    # Whether this workspace may mint a key at all. Lets the page render the
    # upgrade prompt instead of a button that would 403.
    api_access: bool
    agent_api: bool
    available_scopes: list[str]
