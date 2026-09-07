"""Unit of Work — one transaction, all repositories, event dispatch on commit.

Isolation model:
  * Primary guard: every repository query filters by tenant_id explicitly.
  * Backstop: `set_tenant_scope` binds `app.tenant_id` for the transaction so
    Postgres RLS policies (added in migrations) can enforce isolation even if a
    query forgets its filter.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.application.ports.services import EventBus
from src.infrastructure.persistence.database import get_sessionmaker
from src.infrastructure.persistence.repositories import (
    AnalyticsRepositoryImpl,
    ApiKeyRepositoryImpl,
    ApiUsageRepositoryImpl,
    BatchCandidateRepositoryImpl,
    BillingTransactionRepositoryImpl,
    BroadcastRecipientRepositoryImpl,
    BroadcastRepositoryImpl,
    ChatbotRepositoryImpl,
    ChatRepositoryImpl,
    ChunkRepositoryImpl,
    DocumentRepositoryImpl,
    GoogleConnectionRepositoryImpl,
    InterviewBatchRepositoryImpl,
    InterviewRepositoryImpl,
    IssueReportRepositoryImpl,
    OAuthConnectionRepositoryImpl,
    OnboardingRepositoryImpl,
    PostCallConfigRepositoryImpl,
    PostCallDeliveryRepositoryImpl,
    RequestLogRepositoryImpl,
    SubscriptionRepositoryImpl,
    TenantIntegrationRepositoryImpl,
    TenantInviteRepositoryImpl,
    TenantRepositoryImpl,
    UsageRepositoryImpl,
    UserRepositoryImpl,
    VoiceProfileRepositoryImpl,
    WhatsAppChannelRepositoryImpl,
    WhatsAppConversationNoteRepositoryImpl,
    WhatsAppConversationRepositoryImpl,
    WhatsAppWebSessionRepositoryImpl,
)
from src.infrastructure.persistence.scheduling_repositories import (
    AppointmentRepositoryImpl,
    AvailabilityRepositoryImpl,
    LocationRepositoryImpl,
    ReservationRepositoryImpl,
    ResourceRepositoryImpl,
    ServiceRepositoryImpl,
)


class SqlAlchemyUnitOfWork:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker or get_sessionmaker()
        self._event_bus = event_bus
        self._session: AsyncSession | None = None
        self._events: list[object] = []
        self._tenant_id: uuid.UUID | None = None

    async def __aenter__(self) -> SqlAlchemyUnitOfWork:
        self._session = self._sessionmaker()
        await self._session.__aenter__()
        await self._bind_scope()
        self._wire_repositories()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        assert self._session is not None
        if exc_type is not None:
            await self._session.rollback()
        await self._session.__aexit__(exc_type, exc, tb)
        self._session = None

    def _wire_repositories(self) -> None:
        s = self._session
        assert s is not None
        self.tenants = TenantRepositoryImpl(s)
        self.users = UserRepositoryImpl(s)
        self.api_keys = ApiKeyRepositoryImpl(s)
        self.subscriptions = SubscriptionRepositoryImpl(s)
        self.billing_transactions = BillingTransactionRepositoryImpl(s)
        self.api_usage = ApiUsageRepositoryImpl(s)
        self.documents = DocumentRepositoryImpl(s)
        self.chunks = ChunkRepositoryImpl(s)
        self.chatbots = ChatbotRepositoryImpl(s)
        self.chats = ChatRepositoryImpl(s)
        self.usage = UsageRepositoryImpl(s)
        self.analytics = AnalyticsRepositoryImpl(s)
        self.request_logs = RequestLogRepositoryImpl(s)
        self.interviews = InterviewRepositoryImpl(s)
        self.interview_batches = InterviewBatchRepositoryImpl(s)
        self.batch_candidates = BatchCandidateRepositoryImpl(s)
        self.tenant_invites = TenantInviteRepositoryImpl(s)
        self.google_connections = GoogleConnectionRepositoryImpl(s)
        self.oauth_connections = OAuthConnectionRepositoryImpl(s)
        self.whatsapp_channels = WhatsAppChannelRepositoryImpl(s)
        self.whatsapp_conversations = WhatsAppConversationRepositoryImpl(s)
        self.whatsapp_conversation_notes = WhatsAppConversationNoteRepositoryImpl(s)
        self.post_call_configs = PostCallConfigRepositoryImpl(s)
        self.post_call_deliveries = PostCallDeliveryRepositoryImpl(s)
        self.broadcasts = BroadcastRepositoryImpl(s)
        self.broadcast_recipients = BroadcastRecipientRepositoryImpl(s)
        self.tenant_integrations = TenantIntegrationRepositoryImpl(s)
        self.issue_reports = IssueReportRepositoryImpl(s)
        self.onboarding = OnboardingRepositoryImpl(s)
        self.voice_profiles = VoiceProfileRepositoryImpl(s)
        self.whatsapp_web_sessions = WhatsAppWebSessionRepositoryImpl(s)
        # Scheduling. `reservations` is the one that carries the
        # double-booking guarantee — see its module docstring.
        self.locations = LocationRepositoryImpl(s)
        self.services = ServiceRepositoryImpl(s)
        self.resources = ResourceRepositoryImpl(s)
        self.availability = AvailabilityRepositoryImpl(s)
        self.appointments = AppointmentRepositoryImpl(s)
        self.reservations = ReservationRepositoryImpl(s)

    async def _bind_scope(self) -> None:
        if self._session is None or self._tenant_id is None:
            return
        # set_config(..., true) = transaction-local; cleared on commit/rollback.
        await self._session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(self._tenant_id)},
        )

    def set_tenant_scope(self, tenant_id: uuid.UUID | None) -> None:
        # Stored now; bound to the DB session on the next commit (and already
        # bound at __aenter__ when the tenant was known up front). Reads in the
        # same transaction rely on each repository's explicit tenant filter.
        self._tenant_id = tenant_id

    def collect_event(self, event: object) -> None:
        self._events.append(event)

    async def flush(self) -> None:
        assert self._session is not None
        await self._session.flush()

    async def commit(self) -> None:
        assert self._session is not None
        await self._bind_scope()
        await self._session.commit()
        await self._dispatch_events()

    async def rollback(self) -> None:
        assert self._session is not None
        await self._session.rollback()
        self._events.clear()

    async def _dispatch_events(self) -> None:
        if not self._event_bus:
            self._events.clear()
            return
        pending, self._events = self._events, []
        for event in pending:
            await self._event_bus.publish(event)
