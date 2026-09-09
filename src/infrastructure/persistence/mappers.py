"""Map between domain entities and ORM models.

Kept in one place so the translation rules are easy to audit. Domain stays free
of SQLAlchemy; ORM stays free of business logic.
"""

from __future__ import annotations

import uuid

from src.application.ports.repositories import (
    GoogleOAuthConnection,
    OAuthConnection,
    TenantInvite,
    WhatsAppChannel,
    WhatsAppConversation,
    WhatsAppConversationNote,
)
from src.domain.billing.entities import (
    BillingTransaction,
    PlanTier,
    Subscription,
    SubscriptionStatus,
)
from src.domain.broadcast.entities import Broadcast, BroadcastRecipient
from src.domain.chat.entities import ChatSession, Citation, Message, MessageRole
from src.domain.chatbot.entities import (
    RESPONSE_LANGUAGE_AUTO,
    AssistantConfig,
    Chatbot,
    FlowSection,
    RetrievalConfig,
    WidgetConfig,
)
from src.domain.document.entities import Document, IngestionStatus
from src.domain.integration.entities import TenantIntegration
from src.domain.interview.batch_entities import BatchCandidate, InterviewBatch
from src.domain.interview.entities import Interview, QuestionScore, TranscriptTurn
from src.domain.postcall.entities import PostCallConfig, PostCallDelivery
from src.domain.shared.identifiers import (
    BatchCandidateId,
    ChatbotId,
    DocumentId,
    InterviewBatchId,
    InterviewId,
    MessageId,
    SessionId,
    TenantId,
    UserId,
    new_id,
)
from src.domain.support.entities import IssueReport
from src.domain.tenant.entities import ApiKey, Role, Tenant, User
from src.domain.voice.entities import VoiceProfile
from src.domain.whatsapp_web.entities import WhatsAppWebSession
from src.infrastructure.persistence import models as m


def tenant_to_domain(row: m.TenantModel) -> Tenant:
    return Tenant(
        id=TenantId(row.id),
        name=row.name,
        slug=row.slug,
        daily_token_quota=row.daily_token_quota,
        max_documents=row.max_documents,
        is_active=row.is_active,
        created_at=row.created_at,
    )


def user_to_domain(row: m.UserModel) -> User:
    return User(
        id=UserId(row.id),
        email=row.email,
        password_hash=row.password_hash,
        tenant_id=TenantId(row.tenant_id),
        role=Role(row.role),
        is_active=row.is_active,
        is_platform_admin=row.is_platform_admin,
        last_login_at=row.last_login_at,
        created_at=row.created_at,
    )


def apikey_to_domain(row: m.ApiKeyModel) -> ApiKey:
    return ApiKey(
        id=row.id,
        tenant_id=TenantId(row.tenant_id),
        name=row.name,
        key_hash=row.key_hash,
        prefix=row.prefix,
        # `or []` covers rows written before 0034 added the column.
        scopes=list(row.scopes or []),
        plan_tier=row.plan_tier or "free",
        is_active=row.is_active,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
        created_at=row.created_at,
    )


def subscription_to_domain(row: m.SubscriptionModel) -> Subscription:
    return Subscription(
        id=row.id,
        tenant_id=TenantId(row.tenant_id),
        # Coerced through the enums so an unrecognised value from the database
        # cannot become an entitlement the code never intended.
        tier=PlanTier(row.tier) if row.tier in set(PlanTier) else PlanTier.FREE,
        status=(
            SubscriptionStatus(row.status)
            if row.status in set(SubscriptionStatus)
            else SubscriptionStatus.ACTIVE
        ),
        started_at=row.started_at,
        current_period_end=row.current_period_end,
        auto_renew=row.auto_renew,
        canceled_at=row.canceled_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def billing_transaction_to_domain(row: m.BillingTransactionModel) -> BillingTransaction:
    return BillingTransaction(
        id=row.id,
        tenant_id=TenantId(row.tenant_id),
        kind=row.kind,
        amount_usd=row.amount_usd,
        description=row.description,
        plan_tier=row.plan_tier,
        reference=row.reference,
        created_at=row.created_at,
    )


def tenant_invite_to_domain(row: m.TenantInviteModel) -> TenantInvite:
    return TenantInvite(
        id=row.id,
        tenant_id=TenantId(row.tenant_id),
        email=row.email,
        role=row.role,
        token=row.token,
        status=row.status,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


def document_to_domain(row: m.DocumentModel) -> Document:
    return Document(
        id=DocumentId(row.id),
        tenant_id=TenantId(row.tenant_id),
        filename=row.filename,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        storage_key=row.storage_key,
        checksum=row.checksum,
        status=IngestionStatus(row.status),
        chunk_count=row.chunk_count,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def chatbot_to_domain(row: m.ChatbotModel) -> Chatbot:
    rc = row.retrieval or {}
    wc = row.widget_config or {}
    ac = row.assistant_config or {}
    default_widget = WidgetConfig()
    default_assistant = AssistantConfig()
    return Chatbot(
        id=ChatbotId(row.id),
        tenant_id=TenantId(row.tenant_id),
        name=row.name,
        display_id=row.display_id,
        channel=row.channel or "text",  # type: ignore[arg-type]
        system_prompt=row.system_prompt,
        flow_sections=[
            FlowSection(
                id=uuid.UUID(str(s["id"])) if s.get("id") else new_id(),
                title=s.get("title", ""),
                body=s.get("body", ""),
                enabled=bool(s.get("enabled", True)),
            )
            for s in (row.flow_sections or [])
        ],
        retrieval=RetrievalConfig(
            top_k=rc.get("top_k", 5),
            min_score=rc.get("min_score", 0.0),
            rerank=rc.get("rerank", False),
        ),
        assistant=AssistantConfig(
            direction=ac.get("direction", default_assistant.direction),
            languages=list(ac.get("languages") or default_assistant.languages),
            # Deliberately NOT `default_assistant.response_language` ("English
            # (India)"): every row written before this setting existed was
            # running the old unconditional auto-mirror behaviour, on a
            # platform where several tenants' assistants genuinely field
            # Hindi/Hinglish traffic. Falling back to English here would
            # silently change what an already-deployed assistant says to a
            # real customer on the next message, with nobody having asked for
            # that. A brand-new assistant gets English by construction — see
            # `AssistantConfig`'s own default — this fallback only covers rows
            # that predate the field.
            response_language=ac.get("response_language", RESPONSE_LANGUAGE_AUTO),
            tts_voice=ac.get("tts_voice", default_assistant.tts_voice),
            llm_model=ac.get("llm_model", default_assistant.llm_model),
            stt_model=ac.get("stt_model", default_assistant.stt_model),
            welcome_message=ac.get("welcome_message", ""),
            welcome_dynamic=bool(ac.get("welcome_dynamic", True)),
            welcome_interruptible=bool(ac.get("welcome_interruptible", False)),
            appointments_enabled=bool(ac.get("appointments_enabled", False)),
        ),
        voice_profile_id=row.voice_profile_id,
        allowed_document_ids=[DocumentId(uuid.UUID(d)) for d in (row.allowed_document_ids or [])],
        is_public=row.is_public,
        public_key=row.public_key,
        allowed_origins=list(row.allowed_origins or []),
        widget=WidgetConfig(
            theme_color=wc.get("theme_color", default_widget.theme_color),
            display_name=wc.get("display_name", default_widget.display_name),
            welcome_message=wc.get("welcome_message", default_widget.welcome_message),
            launcher_position=wc.get("launcher_position", default_widget.launcher_position),
        ),
        created_at=row.created_at,
    )


def chatbot_retrieval_to_jsonb(rc: RetrievalConfig) -> dict:
    return {"top_k": rc.top_k, "min_score": rc.min_score, "rerank": rc.rerank}


def flow_sections_to_jsonb(sections: list[FlowSection]) -> list[dict]:
    """List order IS section order — preserved by JSONB arrays."""
    return [
        {"id": str(s.id), "title": s.title, "body": s.body, "enabled": s.enabled}
        for s in sections
    ]


def assistant_config_to_jsonb(ac: AssistantConfig) -> dict:
    return {
        "direction": ac.direction,
        "languages": list(ac.languages),
        "response_language": ac.response_language,
        "tts_voice": ac.tts_voice,
        "llm_model": ac.llm_model,
        "stt_model": ac.stt_model,
        "welcome_message": ac.welcome_message,
        "welcome_dynamic": ac.welcome_dynamic,
        "welcome_interruptible": ac.welcome_interruptible,
        "appointments_enabled": ac.appointments_enabled,
    }


def widget_config_to_jsonb(wc: WidgetConfig) -> dict:
    return {
        "theme_color": wc.theme_color,
        "display_name": wc.display_name,
        "welcome_message": wc.welcome_message,
        "launcher_position": wc.launcher_position,
    }


def session_to_domain(row: m.ChatSessionModel) -> ChatSession:
    return ChatSession(
        id=SessionId(row.id),
        tenant_id=TenantId(row.tenant_id),
        chatbot_id=ChatbotId(row.chatbot_id) if row.chatbot_id else None,
        title=row.title,
        created_at=row.created_at,
    )


def message_to_domain(row: m.ChatMessageModel) -> Message:
    return Message(
        id=MessageId(row.id),
        session_id=SessionId(row.session_id),
        tenant_id=TenantId(row.tenant_id),
        role=MessageRole(row.role),
        content=row.content,
        citations=[
            Citation(
                document_id=DocumentId(uuid.UUID(c["document_id"])),
                chunk_id=c["chunk_id"],
                ordinal=c["ordinal"],
                score=c["score"],
                snippet=c["snippet"],
            )
            for c in (row.citations or [])
        ],
        tokens_used=row.tokens_used,
        provider=row.provider,
        created_at=row.created_at,
        media_kind=row.media_kind,
        media_mime_type=row.media_mime_type,
        media_filename=row.media_filename,
        media_storage_key=row.media_storage_key,
        media_size_bytes=row.media_size_bytes,
        provider_message_id=row.provider_message_id,
    )


def citations_to_jsonb(citations: list[Citation]) -> list[dict]:
    return [
        {
            "document_id": str(c.document_id),
            "chunk_id": c.chunk_id,
            "ordinal": c.ordinal,
            "score": c.score,
            "snippet": c.snippet,
        }
        for c in citations
    ]


def interview_to_domain(row: m.InterviewModel) -> Interview:
    return Interview(
        id=InterviewId(row.id),
        tenant_id=TenantId(row.tenant_id),
        candidate_name=row.candidate_name,
        candidate_email=row.candidate_email,
        role_title=row.role_title,
        job_document_id=DocumentId(row.job_document_id),
        resume_document_id=DocumentId(row.resume_document_id),
        scheduled_at=row.scheduled_at,
        window_closes_at=row.window_closes_at,
        status=row.status,  # type: ignore[arg-type]
        access_token=row.access_token,
        questions=list(row.questions or []),
        transcript=[TranscriptTurn(role=t["role"], content=t["content"]) for t in (row.transcript or [])],
        current_question_index=row.current_question_index,
        google_event_id=row.google_event_id,
        calendar_link=row.calendar_link,
        report_storage_key=row.report_storage_key,
        overall_score=row.overall_score,
        overall_verdict=row.overall_verdict,
        scores=[
            QuestionScore(
                question=s["question"],
                answer=s.get("answer", ""),
                score=s["score"],
                justification=s.get("justification", ""),
            )
            for s in (row.scores or [])
        ],
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def transcript_to_jsonb(transcript: list[TranscriptTurn]) -> list[dict]:
    return [{"role": t.role, "content": t.content} for t in transcript]


def scores_to_jsonb(scores: list[QuestionScore]) -> list[dict]:
    return [
        {"question": s.question, "answer": s.answer, "score": s.score, "justification": s.justification}
        for s in scores
    ]


def interview_batch_to_domain(row: m.InterviewBatchModel) -> InterviewBatch:
    return InterviewBatch(
        id=InterviewBatchId(row.id),
        tenant_id=TenantId(row.tenant_id),
        role_title=row.role_title,
        job_document_id=DocumentId(row.job_document_id),
        window_opens_at=row.window_opens_at,
        window_closes_at=row.window_closes_at,
        custom_questions=list(row.custom_questions or []),
        status=row.status,  # type: ignore[arg-type]
        total_count=row.total_count,
        sent_count=row.sent_count,
        failed_count=row.failed_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def batch_candidate_to_domain(row: m.BatchCandidateModel) -> BatchCandidate:
    return BatchCandidate(
        id=BatchCandidateId(row.id),
        tenant_id=TenantId(row.tenant_id),
        batch_id=InterviewBatchId(row.batch_id),
        resume_document_id=DocumentId(row.resume_document_id),
        resume_filename=row.resume_filename,
        candidate_name=row.candidate_name,
        candidate_email=row.candidate_email,
        status=row.status,  # type: ignore[arg-type]
        error=row.error,
        interview_id=InterviewId(row.interview_id) if row.interview_id else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def oauth_connection_to_domain(row: m.OAuthConnectionModel) -> OAuthConnection:
    return OAuthConnection(
        tenant_id=TenantId(row.tenant_id),
        provider=row.provider,
        access_token=row.access_token,
        refresh_token=row.refresh_token,
        expires_at=row.expires_at,
        scope=row.scope,
        account_label=row.account_label,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def google_connection_to_domain(row: m.OAuthConnectionModel) -> GoogleOAuthConnection:
    """The calendar's narrower view of the same row — see OAuthConnection."""
    return GoogleOAuthConnection(
        tenant_id=TenantId(row.tenant_id),
        access_token=row.access_token,
        refresh_token=row.refresh_token,
        expires_at=row.expires_at,
        scope=row.scope,
        connected_email=row.account_label,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def whatsapp_channel_to_domain(row: m.WhatsAppChannelModel) -> WhatsAppChannel:
    return WhatsAppChannel(
        id=row.id,
        tenant_id=TenantId(row.tenant_id),
        chatbot_id=ChatbotId(row.chatbot_id),
        phone_number=row.phone_number,
        provider=row.provider or "twilio",
        twilio_account_sid=row.twilio_account_sid,
        twilio_auth_token=row.twilio_auth_token,
        phone_number_id=row.phone_number_id or "",
        waba_id=row.waba_id or "",
        access_token=row.access_token or "",
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def whatsapp_conversation_to_domain(
    row: m.WhatsAppConversationModel, assignee_email: str = ""
) -> WhatsAppConversation:
    return WhatsAppConversation(
        auto_reply=row.auto_reply,
        id=row.id,
        whatsapp_channel_id=row.whatsapp_channel_id,
        phone_number=row.phone_number,
        session_id=SessionId(row.session_id),
        tenant_id=TenantId(row.tenant_id),
        display_name=row.display_name,
        last_message_at=row.last_message_at,
        last_message_preview=row.last_message_preview,
        unread_count=row.unread_count,
        has_attachment=row.has_attachment,
        awaiting_reply_since=row.awaiting_reply_since,
        followups_sent=row.followups_sent,
        assignee_id=row.assignee_id,
        # Filled by the repository when it joined the assignee in; the mapper
        # only ever sees the row, and a thread whose owner was deleted keeps a
        # blank email rather than a dangling one.
        assignee_email=assignee_email,
        tags=list(row.tags or []),
        pinned=row.pinned,
        status=row.status or "open",
        company=row.company or "",
        job_title=row.job_title or "",
        email=row.email or "",
        city=row.city or "",
        country=row.country or "",
        linkedin_url=row.linkedin_url or "",
        source=row.source or "",
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def whatsapp_conversation_note_to_domain(
    row: m.WhatsAppConversationNoteModel,
) -> WhatsAppConversationNote:
    return WhatsAppConversationNote(
        id=row.id,
        tenant_id=TenantId(row.tenant_id),
        conversation_id=row.conversation_id,
        author_id=row.author_id,
        author_email=row.author_email or "",
        body=row.body,
        created_at=row.created_at,
    )


# --- Post-call delivery ---


def post_call_config_to_domain(row: m.PostCallConfigModel) -> PostCallConfig:
    return PostCallConfig(
        id=row.id,
        tenant_id=TenantId(row.tenant_id),
        chatbot_id=ChatbotId(row.chatbot_id),
        delivery_method=row.delivery_method,  # type: ignore[arg-type]
        webhook_url=row.webhook_url or "",
        email_to=row.email_to or "",
        trigger_statuses=list(row.trigger_statuses or []),
        include_summary=row.include_summary,
        include_transcript=row.include_transcript,
        include_sentiment=row.include_sentiment,
        include_extracted=row.include_extracted,
        enabled=row.enabled,
        created_at=row.created_at,
    )


def post_call_delivery_to_domain(row: m.PostCallDeliveryModel) -> PostCallDelivery:
    return PostCallDelivery(
        id=row.id,
        tenant_id=TenantId(row.tenant_id),
        chatbot_id=ChatbotId(row.chatbot_id),
        config_id=row.config_id,
        session_id=row.session_id,
        call_status=row.call_status,  # type: ignore[arg-type]
        delivery_method=row.delivery_method,  # type: ignore[arg-type]
        destination=row.destination or "",
        status=row.status,
        error=row.error or "",
        payload=dict(row.payload or {}),
        created_at=row.created_at,
    )


# --- Broadcast ---


def broadcast_to_domain(row: m.BroadcastModel) -> Broadcast:
    return Broadcast(
        id=row.id,
        tenant_id=TenantId(row.tenant_id),
        chatbot_id=ChatbotId(row.chatbot_id),
        whatsapp_channel_id=row.whatsapp_channel_id,
        whatsapp_session_id=row.whatsapp_session_id,
        sender_kind=row.sender_kind,  # type: ignore[arg-type]
        mode=row.mode,  # type: ignore[arg-type]
        name=row.name,
        message_template=row.message_template,
        status=row.status,  # type: ignore[arg-type]
        total_count=row.total_count,
        sent_count=row.sent_count,
        delivered_count=row.delivered_count,
        read_count=row.read_count,
        replied_count=row.replied_count,
        failed_count=row.failed_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def broadcast_recipient_to_domain(row: m.BroadcastRecipientModel) -> BroadcastRecipient:
    return BroadcastRecipient(
        id=row.id,
        broadcast_id=row.broadcast_id,
        tenant_id=TenantId(row.tenant_id),
        phone_number=row.phone_number,
        display_name=row.display_name or "",
        status=row.status,  # type: ignore[arg-type]
        error=row.error or "",
        provider_message_id=row.provider_message_id or "",
        session_id=SessionId(row.session_id) if row.session_id else None,
        attempts=row.attempts,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# --- Integrations / support / voice ---


def tenant_integration_to_domain(row: m.TenantIntegrationModel) -> TenantIntegration:
    return TenantIntegration(
        id=row.id,
        tenant_id=TenantId(row.tenant_id),
        integration_id=row.integration_id,
        config=dict(row.config or {}),
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def issue_report_to_domain(row: m.IssueReportModel) -> IssueReport:
    return IssueReport(
        id=row.id,
        tenant_id=TenantId(row.tenant_id),
        name=row.name,
        email=row.email,
        phone=row.phone or "",
        report_type=row.report_type,  # type: ignore[arg-type]
        priority=row.priority,  # type: ignore[arg-type]
        subject=row.subject,
        description=row.description,
        status=row.status,  # type: ignore[arg-type]
        page_url=row.page_url or "",
        user_agent=row.user_agent or "",
        email_sent=row.email_sent,
        created_at=row.created_at,
    )


def voice_profile_to_domain(row: m.VoiceProfileModel) -> VoiceProfile:
    return VoiceProfile(
        id=row.id,
        tenant_id=TenantId(row.tenant_id),
        name=row.name,
        gender=row.gender,  # type: ignore[arg-type]
        language=row.language,
        description=row.description or "",
        sample_storage_key=row.sample_storage_key or "",
        sample_content_type=row.sample_content_type or "",
        sample_bytes=row.sample_bytes,
        duration_seconds=row.duration_seconds,
        provider=row.provider or "",
        provider_voice_id=row.provider_voice_id or "",
        status=row.status,  # type: ignore[arg-type]
        error=row.error or "",
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def whatsapp_web_session_to_domain(row: m.WhatsAppWebSessionModel) -> WhatsAppWebSession:
    return WhatsAppWebSession(
        id=row.id,
        tenant_id=TenantId(row.tenant_id),
        chatbot_id=ChatbotId(row.chatbot_id) if row.chatbot_id else None,
        status=row.status,  # type: ignore[arg-type]
        phone_number=row.phone_number or "",
        display_name=row.display_name or "",
        qr_data_url=row.qr_data_url or "",
        qr_expires_at=row.qr_expires_at,
        last_error=row.last_error or "",
        linked_at=row.linked_at,
        last_seen_at=row.last_seen_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
