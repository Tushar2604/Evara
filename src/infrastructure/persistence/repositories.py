"""SQLAlchemy implementations of the repository ports.

Every query is explicitly filtered by tenant_id (primary isolation guard);
Postgres RLS provides defense-in-depth on top. The vector search uses pgvector
cosine distance computed inside Postgres — no separate vector service.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.ports.repositories import (
    ChatbotDailyStat,
    GoogleOAuthConnection,
    InboxStats,
    OAuthConnection,
    ProviderStat,
    RequestLog,
    TenantInvite,
    WhatsAppChannel,
    WhatsAppConversation,
    WhatsAppConversationNote,
)
from src.domain.billing.entities import BillingTransaction, Subscription
from src.domain.broadcast.entities import Broadcast, BroadcastRecipient
from src.domain.chat.entities import ChatSession, Message
from src.domain.chatbot.entities import Chatbot
from src.domain.document.entities import Chunk, Document, IngestionStatus
from src.domain.integration.entities import TenantIntegration
from src.domain.interview.batch_entities import BatchCandidate, InterviewBatch
from src.domain.interview.entities import Interview
from src.domain.onboarding.entities import Milestones, OnboardingPrefs
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
from src.domain.shared.phone import canonical_phone, phone_digits
from src.domain.support.entities import IssueReport
from src.domain.tenant.entities import ApiKey, Tenant, User
from src.domain.voice.entities import VoiceProfile
from src.domain.whatsapp_web.entities import WhatsAppWebSession
from src.infrastructure.persistence import mappers as map_
from src.infrastructure.persistence import models as m


class TenantRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, tenant: Tenant) -> None:
        self._s.add(
            m.TenantModel(
                id=tenant.id,
                name=tenant.name,
                slug=tenant.slug,
                daily_token_quota=tenant.daily_token_quota,
                max_documents=tenant.max_documents,
                is_active=tenant.is_active,
                created_at=tenant.created_at,
            )
        )

    async def get(self, tenant_id: TenantId) -> Tenant | None:
        row = await self._s.get(m.TenantModel, tenant_id)
        return map_.tenant_to_domain(row) if row else None

    async def get_by_slug(self, slug: str) -> Tenant | None:
        row = (
            await self._s.execute(select(m.TenantModel).where(m.TenantModel.slug == slug))
        ).scalar_one_or_none()
        return map_.tenant_to_domain(row) if row else None

    async def set_limits(
        self, tenant_id: TenantId, *, daily_token_quota: int, max_documents: int
    ) -> None:
        await self._s.execute(
            update(m.TenantModel)
            .where(m.TenantModel.id == tenant_id)
            .values(daily_token_quota=daily_token_quota, max_documents=max_documents)
        )


class UserRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, user: User) -> None:
        self._s.add(
            m.UserModel(
                id=user.id,
                tenant_id=user.tenant_id,
                email=user.email,
                password_hash=user.password_hash,
                role=user.role.value,
                is_active=user.is_active,
                created_at=user.created_at,
            )
        )

    async def get(self, user_id: UserId) -> User | None:
        row = await self._s.get(m.UserModel, user_id)
        return map_.user_to_domain(row) if row else None

    async def set_password_hash(self, user_id: UserId, password_hash: str) -> None:
        """Narrower than a general `update` on purpose: a password reset should
        not be able to change a role or reactivate a disabled account."""
        await self._s.execute(
            update(m.UserModel)
            .where(m.UserModel.id == user_id)
            .values(password_hash=password_hash)
        )

    async def get_by_email(self, email: str) -> User | None:
        row = (
            await self._s.execute(select(m.UserModel).where(m.UserModel.email == email))
        ).scalar_one_or_none()
        return map_.user_to_domain(row) if row else None

    async def list_for_tenant(self, tenant_id: TenantId) -> list[User]:
        rows = (
            await self._s.execute(
                select(m.UserModel)
                .where(m.UserModel.tenant_id == tenant_id)
                .order_by(m.UserModel.created_at.asc())
            )
        ).scalars().all()
        return [map_.user_to_domain(r) for r in rows]


class ApiKeyRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, key: ApiKey) -> None:
        self._s.add(
            m.ApiKeyModel(
                id=key.id,
                tenant_id=key.tenant_id,
                name=key.name,
                key_hash=key.key_hash,
                prefix=key.prefix,
                scopes=list(key.scopes),
                plan_tier=key.plan_tier,
                is_active=key.is_active,
                last_used_at=key.last_used_at,
                revoked_at=key.revoked_at,
                created_at=key.created_at,
            )
        )

    async def get_by_hash(self, key_hash: str) -> ApiKey | None:
        row = (
            await self._s.execute(
                select(m.ApiKeyModel).where(
                    m.ApiKeyModel.key_hash == key_hash, m.ApiKeyModel.is_active.is_(True)
                )
            )
        ).scalar_one_or_none()
        return map_.apikey_to_domain(row) if row else None

    async def get(self, tenant_id: TenantId, key_id: uuid.UUID) -> ApiKey | None:
        row = (
            await self._s.execute(
                select(m.ApiKeyModel).where(
                    m.ApiKeyModel.id == key_id, m.ApiKeyModel.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        return map_.apikey_to_domain(row) if row else None

    async def list_for_tenant(self, tenant_id: TenantId) -> list[ApiKey]:
        rows = (
            await self._s.execute(
                select(m.ApiKeyModel)
                .where(m.ApiKeyModel.tenant_id == tenant_id)
                .order_by(m.ApiKeyModel.created_at.desc())
            )
        ).scalars()
        return [map_.apikey_to_domain(r) for r in rows]

    async def revoke(self, tenant_id: TenantId, key_id: uuid.UUID) -> bool:
        """Deactivate rather than delete.

        A deleted key leaves no answer to "what was calling us last Tuesday",
        and the row is four columns wide. Revocation is what the customer means
        by "delete" and it is immediate: `get_by_hash` only ever returns active
        keys.
        """
        result = await self._s.execute(
            update(m.ApiKeyModel)
            .where(
                m.ApiKeyModel.id == key_id,
                m.ApiKeyModel.tenant_id == tenant_id,
                m.ApiKeyModel.is_active.is_(True),
            )
            .values(is_active=False, revoked_at=datetime.now(UTC))
        )
        return bool(result.rowcount)

    async def touch(self, key_id: uuid.UUID) -> None:
        """Record that the key was used, to the minute.

        Rounded down so a key under sustained load writes one row per minute
        instead of one per request: this column exists to tell a live key from a
        dead one in the dashboard, and second-level precision would cost a write
        on the hot path to answer a question nobody asks.
        """
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        await self._s.execute(
            update(m.ApiKeyModel)
            .where(
                m.ApiKeyModel.id == key_id,
                or_(
                    m.ApiKeyModel.last_used_at.is_(None),
                    m.ApiKeyModel.last_used_at < now,
                ),
            )
            .values(last_used_at=now)
        )


class SubscriptionRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, tenant_id: TenantId) -> Subscription | None:
        row = (
            await self._s.execute(
                select(m.SubscriptionModel).where(m.SubscriptionModel.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        return map_.subscription_to_domain(row) if row else None

    async def upsert(self, subscription: Subscription) -> None:
        row = (
            await self._s.execute(
                select(m.SubscriptionModel).where(
                    m.SubscriptionModel.tenant_id == subscription.tenant_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            self._s.add(
                m.SubscriptionModel(
                    id=subscription.id,
                    tenant_id=subscription.tenant_id,
                    tier=subscription.tier.value,
                    status=subscription.status.value,
                    started_at=subscription.started_at,
                    current_period_end=subscription.current_period_end,
                    auto_renew=subscription.auto_renew,
                    canceled_at=subscription.canceled_at,
                    created_at=subscription.created_at,
                    updated_at=subscription.updated_at,
                )
            )
            return
        row.tier = subscription.tier.value
        row.status = subscription.status.value
        row.started_at = subscription.started_at
        row.current_period_end = subscription.current_period_end
        row.auto_renew = subscription.auto_renew
        row.canceled_at = subscription.canceled_at
        row.updated_at = subscription.updated_at


class BillingTransactionRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, transaction: BillingTransaction) -> None:
        self._s.add(
            m.BillingTransactionModel(
                id=transaction.id,
                tenant_id=transaction.tenant_id,
                kind=transaction.kind,
                amount_usd=transaction.amount_usd,
                description=transaction.description,
                plan_tier=transaction.plan_tier,
                reference=transaction.reference,
                created_at=transaction.created_at,
            )
        )

    async def list_for_tenant(
        self, tenant_id: TenantId, limit: int = 50
    ) -> list[BillingTransaction]:
        rows = (
            await self._s.execute(
                select(m.BillingTransactionModel)
                .where(m.BillingTransactionModel.tenant_id == tenant_id)
                .order_by(m.BillingTransactionModel.created_at.desc())
                .limit(limit)
            )
        ).scalars()
        return [map_.billing_transaction_to_domain(r) for r in rows]


class ApiUsageRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def record_call(self, tenant_id: TenantId) -> None:
        """Increment today's counter.

        An upsert with `ON CONFLICT DO UPDATE` rather than read-then-write:
        several API requests for the same tenant land in the same second
        routinely, and the read-modify-write version loses calls under exactly
        the load where the meter starts to matter.
        """
        today = datetime.now(UTC).date()
        stmt = (
            pg_insert(m.ApiUsageDailyModel)
            .values(id=new_id(), tenant_id=tenant_id, day=today, calls=1)
            .on_conflict_do_update(
                constraint="uq_api_usage_tenant_day",
                set_={"calls": m.ApiUsageDailyModel.calls + 1},
            )
        )
        await self._s.execute(stmt)

    async def calls_since(self, tenant_id: TenantId, since: date) -> int:
        total = (
            await self._s.execute(
                select(func.coalesce(func.sum(m.ApiUsageDailyModel.calls), 0)).where(
                    m.ApiUsageDailyModel.tenant_id == tenant_id,
                    m.ApiUsageDailyModel.day >= since,
                )
            )
        ).scalar_one()
        return int(total or 0)


class TenantInviteRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, invite: TenantInvite) -> None:
        self._s.add(
            m.TenantInviteModel(
                id=invite.id,
                tenant_id=invite.tenant_id,
                email=invite.email,
                role=invite.role,
                token=invite.token,
                status=invite.status,
                created_at=invite.created_at,
                expires_at=invite.expires_at,
            )
        )

    async def get_by_token(self, token: str) -> TenantInvite | None:
        row = (
            await self._s.execute(
                select(m.TenantInviteModel).where(m.TenantInviteModel.token == token)
            )
        ).scalar_one_or_none()
        return map_.tenant_invite_to_domain(row) if row else None

    async def list_for_tenant(self, tenant_id: TenantId) -> list[TenantInvite]:
        rows = (
            await self._s.execute(
                select(m.TenantInviteModel)
                .where(m.TenantInviteModel.tenant_id == tenant_id)
                .order_by(m.TenantInviteModel.created_at.desc())
            )
        ).scalars().all()
        return [map_.tenant_invite_to_domain(r) for r in rows]

    async def mark_accepted(self, invite: TenantInvite) -> None:
        row = await self._s.get(m.TenantInviteModel, invite.id)
        if row is not None:
            row.status = "accepted"


class DocumentRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, document: Document) -> None:
        self._s.add(
            m.DocumentModel(
                id=document.id,
                tenant_id=document.tenant_id,
                filename=document.filename,
                content_type=document.content_type,
                size_bytes=document.size_bytes,
                storage_key=document.storage_key,
                checksum=document.checksum,
                status=document.status.value,
                chunk_count=document.chunk_count,
                error=document.error,
            )
        )

    async def get(self, tenant_id: TenantId, document_id: DocumentId) -> Document | None:
        row = (
            await self._s.execute(
                select(m.DocumentModel).where(
                    m.DocumentModel.id == document_id,
                    m.DocumentModel.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        return map_.document_to_domain(row) if row else None

    async def update(self, document: Document) -> None:
        row = await self._s.get(m.DocumentModel, document.id)
        if row is None:
            return
        row.status = document.status.value
        row.chunk_count = document.chunk_count
        row.error = document.error
        row.checksum = document.checksum
        row.updated_at = document.updated_at

    async def delete(self, tenant_id: TenantId, document_id: DocumentId) -> None:
        await self._s.execute(
            delete(m.DocumentModel).where(
                m.DocumentModel.id == document_id,
                m.DocumentModel.tenant_id == tenant_id,
            )
        )

    async def list_for_tenant(self, tenant_id: TenantId) -> list[Document]:
        rows = (
            await self._s.execute(
                select(m.DocumentModel)
                .where(m.DocumentModel.tenant_id == tenant_id)
                .order_by(m.DocumentModel.created_at.desc())
            )
        ).scalars()
        return [map_.document_to_domain(r) for r in rows]

    async def count_for_tenant(self, tenant_id: TenantId) -> int:
        return (
            await self._s.execute(
                select(func.count())
                .select_from(m.DocumentModel)
                .where(m.DocumentModel.tenant_id == tenant_id)
            )
        ).scalar_one()

    async def list_resumable(self) -> list[Document]:
        # Non-terminal, non-pending docs left mid-pipeline by a restart.
        active = [
            IngestionStatus.UPLOADED.value,
            IngestionStatus.PARSING.value,
            IngestionStatus.CHUNKING.value,
            IngestionStatus.EMBEDDING.value,
        ]
        rows = (
            await self._s.execute(
                select(m.DocumentModel).where(m.DocumentModel.status.in_(active))
            )
        ).scalars()
        return [map_.document_to_domain(r) for r in rows]


class ChunkRepositoryImpl:
    """pgvector-backed vector store."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add_many(self, chunks: list[Chunk], embeddings: list[list[float]]) -> None:
        for chunk, vector in zip(chunks, embeddings, strict=True):
            self._s.add(
                m.ChunkModel(
                    id=uuid.UUID(chunk.id),
                    tenant_id=chunk.tenant_id,
                    document_id=chunk.document_id,
                    ordinal=chunk.ordinal,
                    text=chunk.text,
                    token_estimate=chunk.token_estimate,
                    embedding=vector,
                )
            )

    async def delete_for_document(
        self, tenant_id: TenantId, document_id: DocumentId
    ) -> None:
        await self._s.execute(
            delete(m.ChunkModel).where(
                m.ChunkModel.tenant_id == tenant_id,
                m.ChunkModel.document_id == document_id,
            )
        )

    async def list_for_document(self, tenant_id: TenantId, document_id: DocumentId) -> list[Chunk]:
        rows = (
            await self._s.execute(
                select(m.ChunkModel)
                .where(m.ChunkModel.tenant_id == tenant_id, m.ChunkModel.document_id == document_id)
                .order_by(m.ChunkModel.ordinal)
            )
        ).scalars()
        return [
            Chunk(
                id=str(row.id),
                tenant_id=TenantId(row.tenant_id),
                document_id=DocumentId(row.document_id),
                ordinal=row.ordinal,
                text=row.text,
                token_estimate=row.token_estimate,
            )
            for row in rows
        ]

    async def search(
        self,
        tenant_id: TenantId,
        query_embedding: list[float],
        top_k: int,
        document_ids: list[DocumentId] | None = None,
        min_score: float = 0.0,
    ) -> list[tuple[Chunk, float]]:
        import math

        stmt = select(m.ChunkModel).where(m.ChunkModel.tenant_id == tenant_id)
        # `None` and `[]` mean different things and must not be collapsed:
        # None = unscoped (search the whole tenant), [] = an assistant with an
        # empty knowledge base, which must retrieve nothing rather than
        # everything. `IN ()` yields no rows, which is exactly right.
        if document_ids is not None:
            stmt = stmt.where(m.ChunkModel.document_id.in_(document_ids))

        rows = (await self._s.execute(stmt)).scalars().all()

        def _cosine(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            mag = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(x * x for x in b))
            return dot / mag if mag else 0.0

        scored = [
            (row, _cosine(query_embedding, row.embedding))
            for row in rows
            if row.embedding
        ]
        scored.sort(key=lambda t: t[1], reverse=True)

        results: list[tuple[Chunk, float]] = []
        for row, sim in scored[:top_k]:
            if sim < min_score:
                continue
            results.append((
                Chunk(
                    id=str(row.id),
                    tenant_id=TenantId(row.tenant_id),
                    document_id=DocumentId(row.document_id),
                    ordinal=row.ordinal,
                    text=row.text,
                    token_estimate=row.token_estimate,
                ),
                sim,
            ))
        return results


class ChatbotRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, chatbot: Chatbot) -> None:
        row = _chatbot_to_row(chatbot)
        self._s.add(row)
        # Flush so the sequence-assigned display_id comes back now rather
        # than after commit — the create response carries it, and the UI
        # shows it on the card the moment the assistant appears.
        await self._s.flush()
        chatbot.display_id = row.display_id

    async def get(self, tenant_id: TenantId, chatbot_id: ChatbotId) -> Chatbot | None:
        row = (
            await self._s.execute(
                select(m.ChatbotModel).where(
                    m.ChatbotModel.id == chatbot_id,
                    m.ChatbotModel.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        return map_.chatbot_to_domain(row) if row else None

    async def get_public(self, chatbot_id: ChatbotId) -> Chatbot | None:
        row = (
            await self._s.execute(
                select(m.ChatbotModel).where(
                    m.ChatbotModel.id == chatbot_id, m.ChatbotModel.is_public.is_(True)
                )
            )
        ).scalar_one_or_none()
        return map_.chatbot_to_domain(row) if row else None

    async def get_by_public_key(self, public_key: str) -> Chatbot | None:
        """Resolve a chatbot from its publishable key — intentionally NOT
        tenant-scoped (the widget caller has no tenant context). Only public
        bots are returned, so a key for a private bot resolves to nothing."""
        row = (
            await self._s.execute(
                select(m.ChatbotModel).where(
                    m.ChatbotModel.public_key == public_key,
                    m.ChatbotModel.is_public.is_(True),
                )
            )
        ).scalar_one_or_none()
        return map_.chatbot_to_domain(row) if row else None

    async def delete(self, tenant_id: TenantId, chatbot_id: ChatbotId) -> None:
        """Tenant-scoped so a guessed id cannot reach another workspace's
        assistant. Chat sessions, request logs and per-assistant config cascade;
        a linked WhatsApp number is only detached (SET NULL), because deleting
        an assistant should not silently unlink someone's phone."""
        await self._s.execute(
            delete(m.ChatbotModel).where(
                m.ChatbotModel.id == chatbot_id,
                m.ChatbotModel.tenant_id == tenant_id,
            )
        )

    async def update(self, chatbot: Chatbot) -> None:
        row = await self._s.get(m.ChatbotModel, chatbot.id)
        if row is None:
            return
        row.name = chatbot.name
        row.channel = chatbot.channel
        row.system_prompt = chatbot.system_prompt
        row.flow_sections = map_.flow_sections_to_jsonb(chatbot.flow_sections)
        row.voice_profile_id = chatbot.voice_profile_id
        row.retrieval = map_.chatbot_retrieval_to_jsonb(chatbot.retrieval)
        row.assistant_config = map_.assistant_config_to_jsonb(chatbot.assistant)
        row.allowed_document_ids = [str(d) for d in chatbot.allowed_document_ids]
        row.is_public = chatbot.is_public
        row.public_key = chatbot.public_key
        row.allowed_origins = list(chatbot.allowed_origins)
        row.widget_config = map_.widget_config_to_jsonb(chatbot.widget)

    async def list_for_tenant(self, tenant_id: TenantId) -> list[Chatbot]:
        rows = (
            await self._s.execute(
                select(m.ChatbotModel)
                .where(m.ChatbotModel.tenant_id == tenant_id)
                # Newest first, and ordered at all — this had no ORDER BY, so
                # Postgres was free to return the list differently between two
                # refreshes. Invisible while the cards showed no dates; the
                # moment they do, an unordered list reads as broken.
                .order_by(m.ChatbotModel.created_at.desc())
            )
        ).scalars()
        return [map_.chatbot_to_domain(r) for r in rows]


def _chatbot_to_row(chatbot: Chatbot) -> m.ChatbotModel:
    return m.ChatbotModel(
        id=chatbot.id,
        tenant_id=chatbot.tenant_id,
        name=chatbot.name,
        channel=chatbot.channel,
        system_prompt=chatbot.system_prompt,
        flow_sections=map_.flow_sections_to_jsonb(chatbot.flow_sections),
        voice_profile_id=chatbot.voice_profile_id,
        retrieval=map_.chatbot_retrieval_to_jsonb(chatbot.retrieval),
        assistant_config=map_.assistant_config_to_jsonb(chatbot.assistant),
        allowed_document_ids=[str(d) for d in chatbot.allowed_document_ids],
        is_public=chatbot.is_public,
        public_key=chatbot.public_key,
        allowed_origins=list(chatbot.allowed_origins),
        widget_config=map_.widget_config_to_jsonb(chatbot.widget),
        created_at=chatbot.created_at,
    )


class ChatRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add_session(self, session: ChatSession) -> None:
        self._s.add(
            m.ChatSessionModel(
                id=session.id,
                tenant_id=session.tenant_id,
                chatbot_id=session.chatbot_id,
                title=session.title,
                created_at=session.created_at,
            )
        )

    async def get_session(
        self, tenant_id: TenantId, session_id: SessionId
    ) -> ChatSession | None:
        row = (
            await self._s.execute(
                select(m.ChatSessionModel).where(
                    m.ChatSessionModel.id == session_id,
                    m.ChatSessionModel.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        return map_.session_to_domain(row) if row else None

    async def assign_chatbot(
        self,
        tenant_id: TenantId,
        session_ids: list[SessionId],
        chatbot_id: ChatbotId | None,
    ) -> int:
        """Point existing sessions at a (possibly different) assistant.

        WhatsApp threads are created at the first inbound message, which is
        routinely before the user has picked who answers the number — and the
        answer path reads the *session's* chatbot, not the number's. Without
        this, choosing an assistant only affected threads that started
        afterwards, so every conversation already in the inbox stayed silent.
        Returns the number of rows changed.
        """
        if not session_ids:
            return 0
        result = await self._s.execute(
            update(m.ChatSessionModel)
            .where(
                m.ChatSessionModel.tenant_id == tenant_id,
                m.ChatSessionModel.id.in_(session_ids),
                # Skip rows already correct so the count means "actually moved".
                m.ChatSessionModel.chatbot_id.is_distinct_from(chatbot_id),
            )
            .values(chatbot_id=chatbot_id)
        )
        return int(result.rowcount or 0)

    async def get_booking_state(
        self, tenant_id: TenantId, session_id: SessionId
    ) -> dict | None:
        """The booking agent's working memory for this thread, or None.

        Returned as the raw dict rather than as a domain object: the slate's
        shape belongs to `domain.scheduling.slate`, which parses it defensively
        because a row written by an older revision of that shape must degrade to
        an empty slate, never to a 500 on an inbound message.
        """
        state = (
            await self._s.execute(
                select(m.ChatSessionModel.booking_state).where(
                    m.ChatSessionModel.id == session_id,
                    m.ChatSessionModel.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        return state if isinstance(state, dict) else None

    async def save_booking_state(
        self, tenant_id: TenantId, session_id: SessionId, state: dict | None
    ) -> None:
        """Write the slate back. An empty slate is stored as NULL, so "nothing in
        flight" is one representation rather than two."""
        await self._s.execute(
            update(m.ChatSessionModel)
            .where(
                m.ChatSessionModel.id == session_id,
                m.ChatSessionModel.tenant_id == tenant_id,
            )
            .values(booking_state=state or None)
        )

    async def add_message(self, message: Message) -> None:
        self._s.add(
            m.ChatMessageModel(
                id=message.id,
                tenant_id=message.tenant_id,
                session_id=message.session_id,
                role=message.role.value,
                content=message.content,
                citations=map_.citations_to_jsonb(message.citations),
                tokens_used=message.tokens_used,
                provider=message.provider,
                created_at=message.created_at,
                media_kind=message.media_kind,
                media_mime_type=message.media_mime_type,
                media_filename=message.media_filename,
                media_storage_key=message.media_storage_key,
                media_size_bytes=message.media_size_bytes,
                provider_message_id=message.provider_message_id,
            )
        )

    async def list_messages(
        self,
        tenant_id: TenantId,
        session_id: SessionId,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Message]:
        """Oldest-first history. `limit` takes the *newest* N and returns them
        still in reading order — a thread view wants the tail, not the head,
        and prompt-history callers pass no limit and are unaffected."""
        stmt = select(m.ChatMessageModel).where(
            m.ChatMessageModel.tenant_id == tenant_id,
            m.ChatMessageModel.session_id == session_id,
        )
        if limit is None:
            rows = list(
                (await self._s.execute(stmt.order_by(m.ChatMessageModel.created_at.asc())))
                .scalars()
                .all()
            )
        else:
            rows = list(
                (
                    await self._s.execute(
                        stmt.order_by(m.ChatMessageModel.created_at.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                )
                .scalars()
                .all()
            )[::-1]
        return [map_.message_to_domain(r) for r in rows]

    async def add_messages(self, messages: list[Message]) -> None:
        """Bulk insert. Importing history one row at a time turns a few thousand
        messages into a few thousand round trips against a managed database."""
        if not messages:
            return
        self._s.add_all(
            [
                m.ChatMessageModel(
                    id=msg.id,
                    tenant_id=msg.tenant_id,
                    session_id=msg.session_id,
                    role=msg.role.value,
                    content=msg.content,
                    citations=map_.citations_to_jsonb(msg.citations),
                    tokens_used=msg.tokens_used,
                    provider=msg.provider,
                    created_at=msg.created_at,
                    media_kind=msg.media_kind,
                    media_mime_type=msg.media_mime_type,
                    media_filename=msg.media_filename,
                    media_storage_key=msg.media_storage_key,
                    media_size_bytes=msg.media_size_bytes,
                    provider_message_id=msg.provider_message_id,
                )
                for msg in messages
            ]
        )

    async def existing_provider_ids(
        self, tenant_id: TenantId, session_id: SessionId, provider_ids: list[str]
    ) -> set[str]:
        """Which of these are already stored — one query instead of one per
        message, which is what makes re-running an import cheap."""
        if not provider_ids:
            return set()
        rows = (
            await self._s.execute(
                select(m.ChatMessageModel.provider_message_id).where(
                    m.ChatMessageModel.tenant_id == tenant_id,
                    m.ChatMessageModel.session_id == session_id,
                    m.ChatMessageModel.provider_message_id.in_(provider_ids),
                )
            )
        ).scalars()
        return {r for r in rows if r}

    async def count_messages(self, tenant_id: TenantId, session_id: SessionId) -> int:
        return int(
            (
                await self._s.execute(
                    select(func.count())
                    .select_from(m.ChatMessageModel)
                    .where(
                        m.ChatMessageModel.tenant_id == tenant_id,
                        m.ChatMessageModel.session_id == session_id,
                    )
                )
            ).scalar_one()
        )

    async def message_counts(
        self, tenant_id: TenantId, session_ids: list[SessionId]
    ) -> dict[uuid.UUID, tuple[int, int]]:
        """`{session_id: (messages, attachments)}` for a page of threads.

        One grouped query rather than two per card: the Candidates grid shows
        both numbers on every tile, and a per-thread count would turn a
        30-card page into 60 round trips against a free-tier database.
        """
        if not session_ids:
            return {}
        rows = (
            await self._s.execute(
                select(
                    m.ChatMessageModel.session_id,
                    func.count().label("total"),
                    # `media_kind` is NULL for a plain text message, so a plain
                    # COUNT of it is exactly the attachment tally.
                    func.count(m.ChatMessageModel.media_kind).label("media"),
                )
                .where(
                    m.ChatMessageModel.tenant_id == tenant_id,
                    m.ChatMessageModel.session_id.in_(session_ids),
                )
                .group_by(m.ChatMessageModel.session_id)
            )
        ).all()
        return {row.session_id: (int(row.total), int(row.media)) for row in rows}

    async def message_exists(
        self, tenant_id: TenantId, session_id: SessionId, provider_message_id: str
    ) -> bool:
        """Guards against the WhatsApp socket redelivering on reconnect, which
        would otherwise duplicate every message in the thread."""
        if not provider_message_id:
            return False
        return (
            await self._s.execute(
                select(m.ChatMessageModel.id).where(
                    m.ChatMessageModel.tenant_id == tenant_id,
                    m.ChatMessageModel.session_id == session_id,
                    m.ChatMessageModel.provider_message_id == provider_message_id,
                )
            )
        ).first() is not None


class UsageRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def tokens_used_today(self, tenant_id: TenantId) -> int:
        today = datetime.now(UTC).date()
        val = (
            await self._s.execute(
                select(m.UsageCounterModel.tokens_used).where(
                    m.UsageCounterModel.tenant_id == tenant_id,
                    m.UsageCounterModel.day == today,
                )
            )
        ).scalar_one_or_none()
        return int(val or 0)

    async def add_tokens(self, tenant_id: TenantId, tokens: int) -> None:
        today = datetime.now(UTC).date()
        # Atomic upsert: insert-or-increment in a single statement.
        stmt = (
            pg_insert(m.UsageCounterModel)
            .values(id=uuid.uuid4(), tenant_id=tenant_id, day=today, tokens_used=tokens)
            .on_conflict_do_update(
                index_elements=["tenant_id", "day"],
                set_={"tokens_used": m.UsageCounterModel.tokens_used + tokens},
            )
        )
        await self._s.execute(stmt)


# The canonical off-topic redirect emitted by DEFAULT_SYSTEM_PROMPT.
# Heuristic (custom prompts may refuse differently); no_context_rate is the
# prompt-independent retrieval-miss signal, refusal_rate is the secondary one.
_REFUSAL_LIKE = "I'm here to help with our open roles and your application%"


class AnalyticsRepositoryImpl:
    """Aggregate reads over chat_messages joined to their chatbot. Raw SQL keeps
    the JSONB citation-score extraction readable; still tenant-filtered."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def chatbot_daily(
        self, tenant_id: TenantId, chatbot_id: ChatbotId, since: datetime
    ) -> list[ChatbotDailyStat]:
        sql = text(
            """
            SELECT date_trunc('day', m.created_at)::date            AS day,
                   count(*)                                          AS answers,
                   avg((m.citations->0->>'score')::float)           AS avg_top_score,
                   avg(jsonb_array_length(m.citations))::float      AS avg_citations,
                   avg((jsonb_array_length(m.citations) = 0)::int)::float AS no_context_rate,
                   avg((m.content LIKE :refusal)::int)::float        AS refusal_rate,
                   avg(m.tokens_used)::float                         AS avg_tokens
            FROM chat_messages m
            JOIN chat_sessions s ON s.id = m.session_id
            WHERE m.tenant_id = :tid
              AND m.role = 'assistant'
              AND s.chatbot_id = :cid
              AND m.created_at >= :since
            GROUP BY 1
            ORDER BY 1
            """
        )
        rows = await self._s.execute(
            sql,
            {"tid": tenant_id, "cid": chatbot_id, "since": since, "refusal": _REFUSAL_LIKE},
        )
        return [
            ChatbotDailyStat(
                day=r.day,
                answers=r.answers,
                avg_top_score=r.avg_top_score,
                avg_citations=r.avg_citations or 0.0,
                no_context_rate=r.no_context_rate or 0.0,
                refusal_rate=r.refusal_rate or 0.0,
                avg_tokens=r.avg_tokens or 0.0,
            )
            for r in rows
        ]

    async def chatbot_provider_mix(
        self, tenant_id: TenantId, chatbot_id: ChatbotId, since: datetime
    ) -> list[ProviderStat]:
        sql = text(
            """
            SELECT m.provider                              AS provider,
                   count(*)                                AS answers,
                   avg((m.citations->0->>'score')::float)  AS avg_top_score,
                   avg(m.tokens_used)::float               AS avg_tokens
            FROM chat_messages m
            JOIN chat_sessions s ON s.id = m.session_id
            WHERE m.tenant_id = :tid
              AND m.role = 'assistant'
              AND s.chatbot_id = :cid
              AND m.created_at >= :since
            GROUP BY m.provider
            ORDER BY answers DESC
            """
        )
        rows = await self._s.execute(
            sql, {"tid": tenant_id, "cid": chatbot_id, "since": since}
        )
        return [
            ProviderStat(
                provider=r.provider,
                answers=r.answers,
                avg_top_score=r.avg_top_score,
                avg_tokens=r.avg_tokens or 0.0,
            )
            for r in rows
        ]


def _request_log_to_domain(row: m.RagRequestLogModel) -> RequestLog:
    return RequestLog(
        id=row.id,
        tenant_id=TenantId(row.tenant_id),
        chatbot_id=ChatbotId(row.chatbot_id),
        session_id=SessionId(row.session_id),
        message_id=MessageId(row.message_id) if row.message_id else None,
        query=row.query,
        retrieved=row.retrieved or [],
        num_retrieved=row.num_retrieved,
        max_score=row.max_score,
        no_context=row.no_context,
        refused=row.refused,
        answer=row.answer,
        provider=row.provider,
        model=row.model,
        tokens_used=row.tokens_used,
        status=row.status,
        error=row.error,
        latency_ms=row.latency_ms,
        created_at=row.created_at,
    )


class RequestLogRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, log: RequestLog) -> None:
        self._s.add(
            m.RagRequestLogModel(
                id=log.id,
                tenant_id=log.tenant_id,
                chatbot_id=log.chatbot_id,
                session_id=log.session_id,
                message_id=log.message_id,
                query=log.query,
                retrieved=log.retrieved,
                num_retrieved=log.num_retrieved,
                max_score=log.max_score,
                no_context=log.no_context,
                refused=log.refused,
                answer=log.answer,
                provider=log.provider,
                model=log.model,
                tokens_used=log.tokens_used,
                status=log.status,
                error=log.error,
                latency_ms=log.latency_ms,
                created_at=log.created_at,
            )
        )

    async def list_for_chatbot(
        self, tenant_id: TenantId, chatbot_id: ChatbotId, limit: int = 50
    ) -> list[RequestLog]:
        rows = (
            await self._s.execute(
                select(m.RagRequestLogModel)
                .where(
                    m.RagRequestLogModel.tenant_id == tenant_id,
                    m.RagRequestLogModel.chatbot_id == chatbot_id,
                )
                .order_by(m.RagRequestLogModel.created_at.desc())
                .limit(limit)
            )
        ).scalars()
        return [_request_log_to_domain(r) for r in rows]


class InterviewRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, interview: Interview) -> None:
        self._s.add(_interview_to_row(interview))

    async def get(self, tenant_id: TenantId, interview_id: InterviewId) -> Interview | None:
        row = (
            await self._s.execute(
                select(m.InterviewModel).where(
                    m.InterviewModel.id == interview_id,
                    m.InterviewModel.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        return map_.interview_to_domain(row) if row else None

    async def get_by_token(self, access_token: str) -> Interview | None:
        row = (
            await self._s.execute(
                select(m.InterviewModel).where(m.InterviewModel.access_token == access_token)
            )
        ).scalar_one_or_none()
        return map_.interview_to_domain(row) if row else None

    async def update(self, interview: Interview) -> None:
        row = await self._s.get(m.InterviewModel, interview.id)
        if row is None:
            return
        row.candidate_name = interview.candidate_name
        row.candidate_email = interview.candidate_email
        row.role_title = interview.role_title
        row.scheduled_at = interview.scheduled_at
        row.window_closes_at = interview.window_closes_at
        row.status = interview.status
        row.questions = list(interview.questions)
        row.transcript = map_.transcript_to_jsonb(interview.transcript)
        row.current_question_index = interview.current_question_index
        row.google_event_id = interview.google_event_id
        row.calendar_link = interview.calendar_link
        row.report_storage_key = interview.report_storage_key
        row.overall_score = interview.overall_score
        row.overall_verdict = interview.overall_verdict
        row.scores = map_.scores_to_jsonb(interview.scores)
        row.updated_at = datetime.now(UTC)

    async def list_for_tenant(self, tenant_id: TenantId) -> list[Interview]:
        rows = (
            await self._s.execute(
                select(m.InterviewModel)
                .where(m.InterviewModel.tenant_id == tenant_id)
                .order_by(m.InterviewModel.scheduled_at.desc())
            )
        ).scalars()
        return [map_.interview_to_domain(r) for r in rows]


def _interview_to_row(interview: Interview) -> m.InterviewModel:
    return m.InterviewModel(
        id=interview.id,
        tenant_id=interview.tenant_id,
        candidate_name=interview.candidate_name,
        candidate_email=interview.candidate_email,
        role_title=interview.role_title,
        job_document_id=interview.job_document_id,
        resume_document_id=interview.resume_document_id,
        scheduled_at=interview.scheduled_at,
        window_closes_at=interview.window_closes_at,
        status=interview.status,
        access_token=interview.access_token,
        questions=list(interview.questions),
        transcript=map_.transcript_to_jsonb(interview.transcript),
        current_question_index=interview.current_question_index,
        google_event_id=interview.google_event_id,
        calendar_link=interview.calendar_link,
        report_storage_key=interview.report_storage_key,
        overall_score=interview.overall_score,
        overall_verdict=interview.overall_verdict,
        scores=map_.scores_to_jsonb(interview.scores),
        created_at=interview.created_at,
        updated_at=interview.updated_at,
    )


class InterviewBatchRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, batch: InterviewBatch) -> None:
        self._s.add(
            m.InterviewBatchModel(
                id=batch.id,
                tenant_id=batch.tenant_id,
                role_title=batch.role_title,
                job_document_id=batch.job_document_id,
                window_opens_at=batch.window_opens_at,
                window_closes_at=batch.window_closes_at,
                custom_questions=list(batch.custom_questions),
                status=batch.status,
                total_count=batch.total_count,
                sent_count=batch.sent_count,
                failed_count=batch.failed_count,
                created_at=batch.created_at,
                updated_at=batch.updated_at,
            )
        )

    async def get(self, tenant_id: TenantId, batch_id: InterviewBatchId) -> InterviewBatch | None:
        row = (
            await self._s.execute(
                select(m.InterviewBatchModel).where(
                    m.InterviewBatchModel.id == batch_id,
                    m.InterviewBatchModel.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        return map_.interview_batch_to_domain(row) if row else None

    async def update(self, batch: InterviewBatch) -> None:
        row = await self._s.get(m.InterviewBatchModel, batch.id)
        if row is None:
            return
        row.role_title = batch.role_title
        row.window_opens_at = batch.window_opens_at
        row.window_closes_at = batch.window_closes_at
        row.status = batch.status
        row.total_count = batch.total_count
        row.sent_count = batch.sent_count
        row.failed_count = batch.failed_count
        row.updated_at = datetime.now(UTC)

    async def list_for_tenant(self, tenant_id: TenantId) -> list[InterviewBatch]:
        rows = (
            await self._s.execute(
                select(m.InterviewBatchModel)
                .where(m.InterviewBatchModel.tenant_id == tenant_id)
                .order_by(m.InterviewBatchModel.created_at.desc())
            )
        ).scalars().all()
        return [map_.interview_batch_to_domain(r) for r in rows]

    async def increment_counts(
        self, tenant_id: TenantId, batch_id: InterviewBatchId, *, total: int = 0, sent: int = 0, failed: int = 0
    ) -> None:
        await self._s.execute(
            update(m.InterviewBatchModel)
            .where(
                m.InterviewBatchModel.id == batch_id,
                m.InterviewBatchModel.tenant_id == tenant_id,
            )
            .values(
                total_count=m.InterviewBatchModel.total_count + total,
                sent_count=m.InterviewBatchModel.sent_count + sent,
                failed_count=m.InterviewBatchModel.failed_count + failed,
                updated_at=datetime.now(UTC),
            )
        )


class BatchCandidateRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, candidate: BatchCandidate) -> None:
        self._s.add(_batch_candidate_to_row(candidate))

    async def add_many(self, candidates: list[BatchCandidate]) -> None:
        for candidate in candidates:
            self._s.add(_batch_candidate_to_row(candidate))

    async def get(self, tenant_id: TenantId, candidate_id: BatchCandidateId) -> BatchCandidate | None:
        row = (
            await self._s.execute(
                select(m.BatchCandidateModel).where(
                    m.BatchCandidateModel.id == candidate_id,
                    m.BatchCandidateModel.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        return map_.batch_candidate_to_domain(row) if row else None

    async def update(self, candidate: BatchCandidate) -> None:
        row = await self._s.get(m.BatchCandidateModel, candidate.id)
        if row is None:
            return
        row.candidate_name = candidate.candidate_name
        row.candidate_email = candidate.candidate_email
        row.status = candidate.status
        row.error = candidate.error
        row.interview_id = candidate.interview_id
        row.updated_at = datetime.now(UTC)

    async def list_for_batch(
        self, tenant_id: TenantId, batch_id: InterviewBatchId
    ) -> list[BatchCandidate]:
        rows = (
            await self._s.execute(
                select(m.BatchCandidateModel)
                .where(
                    m.BatchCandidateModel.batch_id == batch_id,
                    m.BatchCandidateModel.tenant_id == tenant_id,
                )
                .order_by(m.BatchCandidateModel.created_at.asc())
            )
        ).scalars().all()
        return [map_.batch_candidate_to_domain(r) for r in rows]


def _batch_candidate_to_row(candidate: BatchCandidate) -> m.BatchCandidateModel:
    return m.BatchCandidateModel(
        id=candidate.id,
        tenant_id=candidate.tenant_id,
        batch_id=candidate.batch_id,
        resume_document_id=candidate.resume_document_id,
        resume_filename=candidate.resume_filename,
        candidate_name=candidate.candidate_name,
        candidate_email=candidate.candidate_email,
        status=candidate.status,
        error=candidate.error,
        interview_id=candidate.interview_id,
        created_at=candidate.created_at,
        updated_at=candidate.updated_at,
    )


class OAuthConnectionRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, tenant_id: TenantId, provider: str) -> OAuthConnection | None:
        row = await self._s.get(m.OAuthConnectionModel, (tenant_id, provider))
        return map_.oauth_connection_to_domain(row) if row else None

    async def list_for_tenant(self, tenant_id: TenantId) -> list[OAuthConnection]:
        rows = (
            await self._s.execute(
                select(m.OAuthConnectionModel).where(
                    m.OAuthConnectionModel.tenant_id == tenant_id
                )
            )
        ).scalars()
        return [map_.oauth_connection_to_domain(r) for r in rows]

    async def upsert(self, connection: OAuthConnection) -> None:
        row = await self._s.get(
            m.OAuthConnectionModel, (connection.tenant_id, connection.provider)
        )
        if row is None:
            self._s.add(
                m.OAuthConnectionModel(
                    tenant_id=connection.tenant_id,
                    provider=connection.provider,
                    access_token=connection.access_token,
                    refresh_token=connection.refresh_token,
                    expires_at=connection.expires_at,
                    scope=connection.scope,
                    account_label=connection.account_label,
                    created_at=connection.created_at,
                    updated_at=connection.updated_at,
                )
            )
            return
        row.access_token = connection.access_token
        # Re-consent does not always return a refresh token. Keeping the stored
        # one rather than blanking it is what stops a reconnect from silently
        # turning a long-lived connection into an hour-long one.
        if connection.refresh_token:
            row.refresh_token = connection.refresh_token
        row.expires_at = connection.expires_at
        row.scope = connection.scope
        if connection.account_label:
            row.account_label = connection.account_label
        row.updated_at = datetime.now(UTC)

    async def delete(self, tenant_id: TenantId, provider: str) -> None:
        row = await self._s.get(m.OAuthConnectionModel, (tenant_id, provider))
        if row is not None:
            await self._s.delete(row)


class GoogleConnectionRepositoryImpl:
    """Interview scheduling's narrower view of `oauth_connections`.

    Calendar code only ever means one provider, so it gets an interface that
    doesn't make it say so on every call. Same rows, same table.
    """

    PROVIDER = "google_calendar"

    def __init__(self, session: AsyncSession) -> None:
        self._s = session
        self._generic = OAuthConnectionRepositoryImpl(session)

    async def get(self, tenant_id: TenantId) -> GoogleOAuthConnection | None:
        row = await self._s.get(m.OAuthConnectionModel, (tenant_id, self.PROVIDER))
        return map_.google_connection_to_domain(row) if row else None

    async def upsert(self, connection: GoogleOAuthConnection) -> None:
        await self._generic.upsert(
            OAuthConnection(
                tenant_id=connection.tenant_id,
                provider=self.PROVIDER,
                access_token=connection.access_token,
                refresh_token=connection.refresh_token,
                expires_at=connection.expires_at,
                scope=connection.scope,
                account_label=connection.connected_email,
                created_at=connection.created_at,
                updated_at=connection.updated_at,
            )
        )

    async def delete(self, tenant_id: TenantId) -> None:
        await self._generic.delete(tenant_id, self.PROVIDER)


class WhatsAppChannelRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, channel: WhatsAppChannel) -> None:
        self._s.add(
            m.WhatsAppChannelModel(
                id=channel.id,
                tenant_id=channel.tenant_id,
                chatbot_id=channel.chatbot_id,
                phone_number=channel.phone_number,
                provider=channel.provider,
                twilio_account_sid=channel.twilio_account_sid,
                twilio_auth_token=channel.twilio_auth_token,
                phone_number_id=channel.phone_number_id,
                waba_id=channel.waba_id,
                access_token=channel.access_token,
                status=channel.status,
                created_at=channel.created_at,
                updated_at=channel.updated_at,
            )
        )

    async def get(self, tenant_id: TenantId, channel_id: uuid.UUID) -> WhatsAppChannel | None:
        row = (
            await self._s.execute(
                select(m.WhatsAppChannelModel).where(
                    m.WhatsAppChannelModel.id == channel_id,
                    m.WhatsAppChannelModel.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        return map_.whatsapp_channel_to_domain(row) if row else None

    async def get_by_chatbot(
        self, tenant_id: TenantId, chatbot_id: ChatbotId
    ) -> WhatsAppChannel | None:
        row = (
            await self._s.execute(
                select(m.WhatsAppChannelModel).where(
                    m.WhatsAppChannelModel.chatbot_id == chatbot_id,
                    m.WhatsAppChannelModel.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        return map_.whatsapp_channel_to_domain(row) if row else None

    async def get_by_phone_number(self, phone_number: str) -> WhatsAppChannel | None:
        row = (
            await self._s.execute(
                select(m.WhatsAppChannelModel).where(
                    m.WhatsAppChannelModel.phone_number == phone_number
                )
            )
        ).scalar_one_or_none()
        return map_.whatsapp_channel_to_domain(row) if row else None

    async def get_by_phone_number_id(self, phone_number_id: str) -> WhatsAppChannel | None:
        # Guarded against the empty string: every Twilio row carries one, and a
        # blank lookup would otherwise return an arbitrary tenant's channel.
        if not phone_number_id:
            return None
        row = (
            await self._s.execute(
                select(m.WhatsAppChannelModel).where(
                    m.WhatsAppChannelModel.phone_number_id == phone_number_id
                )
            )
        ).scalar_one_or_none()
        return map_.whatsapp_channel_to_domain(row) if row else None

    async def list_for_tenant(self, tenant_id: TenantId) -> list[WhatsAppChannel]:
        rows = (
            await self._s.execute(
                select(m.WhatsAppChannelModel).where(m.WhatsAppChannelModel.tenant_id == tenant_id)
            )
        ).scalars()
        return [map_.whatsapp_channel_to_domain(r) for r in rows]

    async def delete(self, tenant_id: TenantId, channel_id: uuid.UUID) -> None:
        row = (
            await self._s.execute(
                select(m.WhatsAppChannelModel).where(
                    m.WhatsAppChannelModel.id == channel_id,
                    m.WhatsAppChannelModel.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            await self._s.delete(row)


def _DIGITS_ONLY(column):  # type: ignore[no-untyped-def]
    """`+971 50 123 4567` -> `971501234567`, in SQL.

    Not indexable, and deliberately not indexed: it is only used to look up one
    contact within one number's threads, which is a handful of rows, and an
    expression index here would have to be maintained on every inbound message.
    """
    return func.regexp_replace(column, r"[^0-9]", "", "g")


class WhatsAppConversationRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(
        self, whatsapp_channel_id: uuid.UUID, phone_number: str
    ) -> WhatsAppConversation | None:
        """The thread for one contact on one number.

        Matched on digits rather than on the stored string. Writers now
        canonicalise (see `domain.shared.phone`), but rows written before that
        are still in whatever shape their writer used, and an exact-match
        lookup against those is what created a second thread — and a second
        Candidates entry — for a contact who already had one.

        Ordered oldest-first so that where duplicates do still exist, every
        lookup resolves to the same one: the thread that holds the history.
        """
        digits = phone_digits(phone_number)
        match = (
            _DIGITS_ONLY(m.WhatsAppConversationModel.phone_number) == digits
            if digits
            # Nothing usable to compare on — fall back to the literal key so an
            # oddly-addressed thread still finds itself instead of being
            # re-created on every message.
            else m.WhatsAppConversationModel.phone_number == phone_number
        )
        row = (
            await self._s.execute(
                select(m.WhatsAppConversationModel)
                .where(
                    m.WhatsAppConversationModel.whatsapp_channel_id == whatsapp_channel_id,
                    match,
                )
                .order_by(m.WhatsAppConversationModel.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return map_.whatsapp_conversation_to_domain(row) if row else None

    async def add(self, conversation: WhatsAppConversation) -> None:
        self._s.add(
            m.WhatsAppConversationModel(
                id=conversation.id,
                whatsapp_channel_id=conversation.whatsapp_channel_id,
                phone_number=conversation.phone_number,
                session_id=conversation.session_id,
                tenant_id=conversation.tenant_id,
                auto_reply=conversation.auto_reply,
                display_name=conversation.display_name,
                last_message_at=conversation.last_message_at,
                last_message_preview=conversation.last_message_preview,
                unread_count=conversation.unread_count,
                has_attachment=conversation.has_attachment,
                awaiting_reply_since=conversation.awaiting_reply_since,
                followups_sent=conversation.followups_sent,
                assignee_id=conversation.assignee_id,
                tags=list(conversation.tags),
                pinned=conversation.pinned,
                status=conversation.status,
                company=conversation.company,
                job_title=conversation.job_title,
                email=conversation.email,
                city=conversation.city,
                country=conversation.country,
                linkedin_url=conversation.linkedin_url,
                source=conversation.source,
                created_at=conversation.created_at,
                updated_at=conversation.updated_at,
            )
        )

    async def update(self, conversation: WhatsAppConversation) -> None:
        await self._s.execute(
            update(m.WhatsAppConversationModel)
            .where(m.WhatsAppConversationModel.id == conversation.id)
            .values(
                auto_reply=conversation.auto_reply,
                display_name=conversation.display_name,
                last_message_at=conversation.last_message_at,
                last_message_preview=conversation.last_message_preview,
                unread_count=conversation.unread_count,
                has_attachment=conversation.has_attachment,
                awaiting_reply_since=conversation.awaiting_reply_since,
                followups_sent=conversation.followups_sent,
                assignee_id=conversation.assignee_id,
                tags=list(conversation.tags),
                pinned=conversation.pinned,
                status=conversation.status,
                company=conversation.company,
                job_title=conversation.job_title,
                email=conversation.email,
                city=conversation.city,
                country=conversation.country,
                linkedin_url=conversation.linkedin_url,
                source=conversation.source,
                updated_at=conversation.updated_at,
            )
        )

    # The thread list shows who owns each conversation, and an id is not a
    # name. Outer-joined rather than looked up per row: this is the query that
    # renders 50 threads at once, and 50 follow-up SELECTs is how a list gets
    # slow for no visible reason.
    _ASSIGNEE_JOIN = (m.UserModel, m.WhatsAppConversationModel.assignee_id == m.UserModel.id)

    def _rows_to_domain(self, result) -> list[WhatsAppConversation]:  # type: ignore[no-untyped-def]
        return [
            map_.whatsapp_conversation_to_domain(row, email or "")
            for row, email in result.all()
        ]

    async def get_by_id(
        self, tenant_id: TenantId, conversation_id: uuid.UUID
    ) -> WhatsAppConversation | None:
        row = (
            await self._s.execute(
                select(m.WhatsAppConversationModel, m.UserModel.email)
                .outerjoin(*self._ASSIGNEE_JOIN)
                .where(
                    m.WhatsAppConversationModel.id == conversation_id,
                    m.WhatsAppConversationModel.tenant_id == tenant_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return map_.whatsapp_conversation_to_domain(row[0], row[1] or "")

    async def lock_for_follow_up(
        self, tenant_id: TenantId, conversation_id: uuid.UUID
    ) -> WhatsAppConversation | None:
        # No join here, deliberately: `FOR UPDATE` cannot be combined with an
        # outer join in Postgres, and this caller only needs the follow-up
        # fields — not the assignee email `get_by_id` joins in for the inbox.
        row = (
            await self._s.execute(
                select(m.WhatsAppConversationModel)
                .where(
                    m.WhatsAppConversationModel.id == conversation_id,
                    m.WhatsAppConversationModel.tenant_id == tenant_id,
                )
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        return map_.whatsapp_conversation_to_domain(row) if row else None

    def _owner_filters(
        self,
        tenant_id: TenantId,
        owner_id: uuid.UUID | None,
        search: str,
        has_attachment: bool | None,
        unread_only: bool,
        auto_reply: bool | None,
        *,
        assignee_id: uuid.UUID | None = None,
        unassigned: bool = False,
        status: str = "",
        pinned: bool | None = None,
        tag: str = "",
    ) -> list:
        """Shared WHERE clauses so the page query and its count can never drift
        apart — a mismatch there shows the wrong page total, which reads as data
        loss to whoever is looking at the inbox.

        `owner_id=None` drops the per-number filter entirely — the tenant-wide
        Candidates view over every number, rather than the one-number inbox."""
        conditions = [m.WhatsAppConversationModel.tenant_id == tenant_id]
        if owner_id is not None:
            conditions.append(m.WhatsAppConversationModel.whatsapp_channel_id == owner_id)
        if search:
            like = f"%{search.strip()}%"
            conditions.append(
                or_(
                    m.WhatsAppConversationModel.phone_number.ilike(like),
                    m.WhatsAppConversationModel.display_name.ilike(like),
                    m.WhatsAppConversationModel.last_message_preview.ilike(like),
                )
            )
        if has_attachment is not None:
            conditions.append(m.WhatsAppConversationModel.has_attachment.is_(has_attachment))
        if unread_only:
            conditions.append(m.WhatsAppConversationModel.unread_count > 0)
        if auto_reply is not None:
            conditions.append(m.WhatsAppConversationModel.auto_reply.is_(auto_reply))
        if assignee_id is not None:
            conditions.append(m.WhatsAppConversationModel.assignee_id == assignee_id)
        if unassigned:
            conditions.append(m.WhatsAppConversationModel.assignee_id.is_(None))
        if status:
            conditions.append(m.WhatsAppConversationModel.status == status)
        if pinned is not None:
            conditions.append(m.WhatsAppConversationModel.pinned.is_(pinned))
        if tag:
            # Containment against a JSONB array, so a tag is matched exactly
            # rather than as a substring of another tag ("Hot" vs "Hot Lead").
            conditions.append(m.WhatsAppConversationModel.tags.contains([tag]))
        return conditions

    async def list_for_owner(
        self,
        tenant_id: TenantId,
        owner_id: uuid.UUID,
        *,
        search: str = "",
        has_attachment: bool | None = None,
        unread_only: bool = False,
        auto_reply: bool | None = None,
        assignee_id: uuid.UUID | None = None,
        unassigned: bool = False,
        status: str = "",
        pinned: bool | None = None,
        tag: str = "",
        limit: int = 30,
        offset: int = 0,
    ) -> list[WhatsAppConversation]:
        result = await self._s.execute(
            select(m.WhatsAppConversationModel, m.UserModel.email)
            .outerjoin(*self._ASSIGNEE_JOIN)
            .where(
                *self._owner_filters(
                    tenant_id,
                    owner_id,
                    search,
                    has_attachment,
                    unread_only,
                    auto_reply,
                    assignee_id=assignee_id,
                    unassigned=unassigned,
                    status=status,
                    pinned=pinned,
                    tag=tag,
                )
            )
            # Pinned threads first — that is the whole point of pinning one —
            # then newest activity, with threads that have never had a message
            # sinking to the bottom rather than sorting randomly.
            .order_by(
                m.WhatsAppConversationModel.pinned.desc(),
                m.WhatsAppConversationModel.last_message_at.desc().nullslast(),
                m.WhatsAppConversationModel.created_at.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        return self._rows_to_domain(result)

    async def count_for_owner(
        self,
        tenant_id: TenantId,
        owner_id: uuid.UUID,
        *,
        search: str = "",
        has_attachment: bool | None = None,
        unread_only: bool = False,
        auto_reply: bool | None = None,
        assignee_id: uuid.UUID | None = None,
        unassigned: bool = False,
        status: str = "",
        pinned: bool | None = None,
        tag: str = "",
    ) -> int:
        return int(
            (
                await self._s.execute(
                    select(func.count())
                    .select_from(m.WhatsAppConversationModel)
                    .where(
                        *self._owner_filters(
                            tenant_id,
                            owner_id,
                            search,
                            has_attachment,
                            unread_only,
                            auto_reply,
                            assignee_id=assignee_id,
                            unassigned=unassigned,
                            status=status,
                            pinned=pinned,
                            tag=tag,
                        )
                    )
                )
            ).scalar_one()
        )

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
    ) -> list[WhatsAppConversation]:
        rows = (
            
                await self._s.execute(
                    select(m.WhatsAppConversationModel, m.UserModel.email)
                    .outerjoin(*self._ASSIGNEE_JOIN)
                    .where(
                        *self._owner_filters(
                            tenant_id, None, search, has_attachment, unread_only, auto_reply
                        )
                    )
                    .order_by(
                        m.WhatsAppConversationModel.pinned.desc(),
                        m.WhatsAppConversationModel.last_message_at.desc().nullslast(),
                        m.WhatsAppConversationModel.created_at.desc(),
                    )
                    .limit(limit)
                    .offset(offset)
                )
            
        )
        return self._rows_to_domain(rows)

    async def count_for_tenant(
        self,
        tenant_id: TenantId,
        *,
        search: str = "",
        has_attachment: bool | None = None,
        unread_only: bool = False,
        auto_reply: bool | None = None,
    ) -> int:
        return int(
            (
                await self._s.execute(
                    select(func.count())
                    .select_from(m.WhatsAppConversationModel)
                    .where(
                        *self._owner_filters(
                            tenant_id, None, search, has_attachment, unread_only, auto_reply
                        )
                    )
                )
            ).scalar_one()
        )

    async def list_due_follow_ups(
        self, *, cutoff: datetime, max_follow_ups: int, limit: int = 50
    ) -> list[WhatsAppConversation]:
        """Conversations that have gone quiet long enough to deserve a nudge.

        Deliberately not tenant-scoped: this backs a system sweep that runs on
        a timer with no request and no principal behind it, the same shape as
        the bridge's `get_unscoped` lookups. Every row it returns still carries
        its own `tenant_id`, and the caller scopes each send to that tenant.

        Ordered oldest-first so the contact who has been waiting longest is
        served first when a backlog builds up after the host has been asleep.

        `FOR UPDATE SKIP LOCKED` keeps two simultaneous readers from picking up
        the same slice. It is only half the guard, though: these locks live for
        the duration of *this* transaction, which commits before any message is
        sent, so it cannot by itself stop two processes double-nudging a
        contact. The cross-process guard is the advisory lock the sweep loop
        takes around the whole tick (see `_follow_up_loop` in the API app) —
        this clause just keeps concurrent readers from colliding underneath it.
        """
        rows = (
            (
                await self._s.execute(
                    select(m.WhatsAppConversationModel)
                    .where(
                        m.WhatsAppConversationModel.awaiting_reply_since.is_not(None),
                        m.WhatsAppConversationModel.awaiting_reply_since <= cutoff,
                        m.WhatsAppConversationModel.auto_reply.is_(True),
                        # The sign-off is the (max + 1)th message, so a thread
                        # is still owed one when it is exactly at the limit.
                        m.WhatsAppConversationModel.followups_sent <= max_follow_ups,
                    )
                    .order_by(m.WhatsAppConversationModel.awaiting_reply_since.asc())
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        return [map_.whatsapp_conversation_to_domain(r) for r in rows]

    async def reassign_owner(
        self, tenant_id: TenantId, from_owner_id: uuid.UUID, to_owner_id: uuid.UUID
    ) -> int:
        """Fold one owner's threads into another's. See the port for why.

        Threads whose phone number the target already has are deleted, not
        moved: `uq_whatsapp_conv_channel_phone` would reject the UPDATE, and the
        target's copy is the one the live socket is writing to. Their messages
        live on `chat_sessions` and are unaffected — this only drops the empty
        pointer row that the re-scan created.
        """
        if from_owner_id == to_owner_id:
            return 0

        # A contact who messaged both sessions has a thread on each. Their
        # history is folded together rather than one copy being dropped: the
        # whole point of merging a re-scanned number is that the conversation
        # comes back whole, and deleting the older half would lose exactly the
        # messages the user is looking for.
        merged = await self.merge_duplicate_threads(
            tenant_id, owner_ids=[from_owner_id, to_owner_id], keep_owner_id=to_owner_id
        )
        moved = await self._s.execute(
            update(m.WhatsAppConversationModel)
            .where(
                m.WhatsAppConversationModel.tenant_id == tenant_id,
                m.WhatsAppConversationModel.whatsapp_channel_id == from_owner_id,
            )
            .values(whatsapp_channel_id=to_owner_id)
        )
        return int(moved.rowcount or 0) + merged

    # --- Candidates: one row per person, not per thread ---------------------
    #
    # Candidates is a tenant-wide view, and a contact who reached two connected
    # numbers legitimately has a thread on each. Listing threads therefore
    # listed the same person twice — which is the other half of "the same
    # number in three places", and the half that must NOT be fixed by deleting
    # anything: both conversations are real, and the user wants to pick which
    # number they are looking at.
    #
    # So the grouping happens at read time, in SQL rather than over a page of
    # results: grouping a page would still show a duplicate whenever the two
    # copies fell either side of a page boundary, and would make `total` a lie.

    def _contact_key(self):  # type: ignore[no-untyped-def]
        """What makes two threads the same person: the digits of their number,
        falling back to the raw key for anything that was never a number."""
        digits = _DIGITS_ONLY(m.WhatsAppConversationModel.phone_number)
        return func.coalesce(func.nullif(digits, ""), m.WhatsAppConversationModel.phone_number)

    async def list_contacts_for_tenant(
        self,
        tenant_id: TenantId,
        *,
        search: str = "",
        has_attachment: bool | None = None,
        unread_only: bool = False,
        owner_id: uuid.UUID | None = None,
        limit: int = 30,
        offset: int = 0,
    ) -> list[WhatsAppConversation]:
        """One conversation per contact — the newest-active thread they have.

        `owner_id` narrows to a single connected number, which is how the
        Candidates page offers "show me the people on THIS WhatsApp number".
        """
        key = self._contact_key()
        conditions = self._owner_filters(
            tenant_id, owner_id, search, has_attachment, unread_only, None
        )
        # DISTINCT ON needs its ORDER BY to lead with the same expression, so
        # the choice of representative is made in an inner query and re-sorted
        # outside it.
        inner = (
            select(m.WhatsAppConversationModel.id)
            .distinct(key)
            .where(*conditions)
            .order_by(
                key,
                # The thread they are most recently active on represents them;
                # a contact who moved to a second number should not be shown
                # under the one they abandoned.
                m.WhatsAppConversationModel.last_message_at.desc().nullslast(),
                m.WhatsAppConversationModel.created_at.asc(),
            )
            .scalar_subquery()
        )
        result = await self._s.execute(
            select(m.WhatsAppConversationModel, m.UserModel.email)
            .outerjoin(*self._ASSIGNEE_JOIN)
            .where(m.WhatsAppConversationModel.id.in_(inner))
            .order_by(
                m.WhatsAppConversationModel.last_message_at.desc().nullslast(),
                m.WhatsAppConversationModel.created_at.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        return self._rows_to_domain(result)

    async def count_contacts_for_tenant(
        self,
        tenant_id: TenantId,
        *,
        search: str = "",
        has_attachment: bool | None = None,
        unread_only: bool = False,
        owner_id: uuid.UUID | None = None,
    ) -> int:
        """People, not threads — so the total agrees with what the list shows."""
        return int(
            (
                await self._s.execute(
                    select(func.count(func.distinct(self._contact_key()))).where(
                        *self._owner_filters(
                            tenant_id, owner_id, search, has_attachment, unread_only, None
                        )
                    )
                )
            ).scalar_one()
        )

    async def threads_for_contact(
        self, tenant_id: TenantId, phone_number: str
    ) -> list[WhatsAppConversation]:
        """Every thread this person has, across every connected number.

        What the profile needs to offer "you have also spoken to them on this
        other number" — the switch the user asked for, rather than a merge that
        would throw one of the two conversations away.
        """
        digits = phone_digits(phone_number)
        match = (
            _DIGITS_ONLY(m.WhatsAppConversationModel.phone_number) == digits
            if digits
            else m.WhatsAppConversationModel.phone_number == phone_number
        )
        result = await self._s.execute(
            select(m.WhatsAppConversationModel, m.UserModel.email)
            .outerjoin(*self._ASSIGNEE_JOIN)
            .where(m.WhatsAppConversationModel.tenant_id == tenant_id, match)
            .order_by(m.WhatsAppConversationModel.last_message_at.desc().nullslast())
        )
        return self._rows_to_domain(result)

    async def merge_duplicate_threads(
        self,
        tenant_id: TenantId,
        *,
        owner_ids: list[uuid.UUID],
        keep_owner_id: uuid.UUID | None = None,
    ) -> int:
        """Collapse threads that are the same contact into one, and say how many
        were absorbed.

        Two rows are the same contact when their numbers have the same digits.
        Which one survives is decided in this order: the one on `keep_owner_id`
        if given (the session holding the live socket), then the oldest — the
        thread that has been accumulating history the longest, and the one whose
        id is already in somebody's browser history.

        The loser's messages are re-pointed at the winner's chat session, so
        this genuinely merges the conversation instead of discarding half of it.
        The emptied chat session is then deleted, which is what removes the
        duplicate from Candidates.
        """
        if not owner_ids:
            return 0
        rows = (
            (
                await self._s.execute(
                    select(m.WhatsAppConversationModel)
                    .where(
                        m.WhatsAppConversationModel.tenant_id == tenant_id,
                        m.WhatsAppConversationModel.whatsapp_channel_id.in_(owner_ids),
                    )
                    .order_by(m.WhatsAppConversationModel.created_at.asc())
                )
            )
            .scalars()
            .all()
        )

        groups: dict[str, list] = {}
        for row in rows:
            key = phone_digits(row.phone_number) or row.phone_number.strip()
            if key:
                groups.setdefault(key, []).append(row)

        absorbed = 0
        for group in groups.values():
            if len(group) < 2:
                continue
            keeper = next(
                (r for r in group if keep_owner_id and r.whatsapp_channel_id == keep_owner_id),
                group[0],
            )
            for loser in group:
                if loser.id == keeper.id:
                    continue
                await self._absorb_thread(tenant_id, keeper, loser)
                absorbed += 1
        return absorbed

    async def _absorb_thread(self, tenant_id: TenantId, keeper, loser) -> None:  # type: ignore[no-untyped-def]
        """Move one duplicate thread's messages onto the thread that survives."""
        await self._s.execute(
            update(m.ChatMessageModel)
            .where(
                m.ChatMessageModel.tenant_id == tenant_id,
                m.ChatMessageModel.session_id == loser.session_id,
            )
            .values(session_id=keeper.session_id)
        )

        # Fold the denormalised list metadata rather than recomputing it: the
        # counters exist so the thread list renders in one query, and a merge
        # that left them stale would show the wrong preview and a wrong unread
        # badge until the next message happened to arrive.
        keeper.unread_count = (keeper.unread_count or 0) + (loser.unread_count or 0)
        keeper.has_attachment = bool(keeper.has_attachment or loser.has_attachment)
        if loser.last_message_at and (
            keeper.last_message_at is None or loser.last_message_at > keeper.last_message_at
        ):
            keeper.last_message_at = loser.last_message_at
            keeper.last_message_preview = loser.last_message_preview
        # A name, a company, an owner or a tag recorded on either copy is
        # something a person entered. Keeping whichever copy has it means the
        # merge never costs the user work they already did.
        for field in (
            "display_name", "company", "job_title", "email", "city", "country",
            "linkedin_url", "source",
        ):
            if not getattr(keeper, field) and getattr(loser, field):
                setattr(keeper, field, getattr(loser, field))
        if keeper.assignee_id is None and loser.assignee_id is not None:
            keeper.assignee_id = loser.assignee_id
        merged_tags = list(keeper.tags or [])
        seen = {t.casefold() for t in merged_tags}
        for tag in loser.tags or []:
            if tag.casefold() not in seen:
                seen.add(tag.casefold())
                merged_tags.append(tag)
        keeper.tags = merged_tags
        keeper.pinned = bool(keeper.pinned or loser.pinned)
        keeper.phone_number = canonical_phone(keeper.phone_number)
        keeper.updated_at = datetime.now(UTC)

        # Notes follow their thread; they are the team's own record and must not
        # be lost with the row they happened to be written on.
        await self._s.execute(
            update(m.WhatsAppConversationNoteModel)
            .where(
                m.WhatsAppConversationNoteModel.tenant_id == tenant_id,
                m.WhatsAppConversationNoteModel.conversation_id == loser.id,
            )
            .values(conversation_id=keeper.id)
        )
        await self._s.execute(
            delete(m.WhatsAppConversationModel).where(
                m.WhatsAppConversationModel.id == loser.id
            )
        )
        # The emptied session goes too — its messages now belong to the keeper,
        # and leaving it behind is what would keep a ghost in any view that
        # counts chat sessions.
        await self._s.execute(
            delete(m.ChatSessionModel).where(m.ChatSessionModel.id == loser.session_id)
        )

    async def stats_for_owners(
        self, tenant_id: TenantId, owner_ids: list[uuid.UUID], *, since: datetime
    ) -> InboxStats:
        if not owner_ids:
            return InboxStats()

        conv = m.WhatsAppConversationModel
        scope = [conv.tenant_id == tenant_id, conv.whatsapp_channel_id.in_(owner_ids)]

        # One pass over the conversation rows for every thread-shaped counter.
        # Separate queries would each re-scan the same rows for a header that
        # refreshes on a poll.
        row = (
            await self._s.execute(
                select(
                    func.count(),
                    func.count().filter(conv.status == "open"),
                    func.coalesce(func.sum(conv.unread_count), 0),
                ).where(*scope)
            )
        ).one()

        # Message counters come from the chat sessions those threads point at.
        # "This month" is the caller's `since`, so the window is stated in one
        # place rather than assumed at both ends.
        msg = m.ChatMessageModel
        sessions = select(conv.session_id).where(*scope).scalar_subquery()
        sent, received = (
            await self._s.execute(
                select(
                    func.count().filter(msg.role == "assistant"),
                    func.count().filter(msg.role == "user"),
                ).where(
                    msg.tenant_id == tenant_id,
                    msg.session_id.in_(sessions),
                    msg.created_at >= since,
                )
            )
        ).one()

        # Reply rate, honestly defined: of the threads we have written to at
        # all, how many has the contact answered on. Counted over the same
        # window so it moves with the other figures.
        contacted, replied = (
            await self._s.execute(
                select(
                    func.count(func.distinct(msg.session_id)).filter(msg.role == "assistant"),
                    func.count(func.distinct(msg.session_id)).filter(msg.role == "user"),
                ).where(
                    msg.tenant_id == tenant_id,
                    msg.session_id.in_(sessions),
                    msg.created_at >= since,
                )
            )
        ).one()

        return InboxStats(
            conversations=int(row[0] or 0),
            active_conversations=int(row[1] or 0),
            unread=int(row[2] or 0),
            messages_sent=int(sent or 0),
            messages_received=int(received or 0),
            threads_contacted=int(contacted or 0),
            threads_replied=int(replied or 0),
        )


class WhatsAppConversationNoteRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, note: WhatsAppConversationNote) -> None:
        self._s.add(
            m.WhatsAppConversationNoteModel(
                id=note.id,
                tenant_id=note.tenant_id,
                conversation_id=note.conversation_id,
                author_id=note.author_id,
                author_email=note.author_email,
                body=note.body,
                created_at=note.created_at,
            )
        )

    async def list_for_conversation(
        self, tenant_id: TenantId, conversation_id: uuid.UUID, *, limit: int = 100
    ) -> list[WhatsAppConversationNote]:
        rows = (
            (
                await self._s.execute(
                    select(m.WhatsAppConversationNoteModel)
                    .where(
                        m.WhatsAppConversationNoteModel.tenant_id == tenant_id,
                        m.WhatsAppConversationNoteModel.conversation_id == conversation_id,
                    )
                    # Newest first: a note panel is read from the top, and the
                    # note someone just added is the one they are looking for.
                    .order_by(m.WhatsAppConversationNoteModel.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [map_.whatsapp_conversation_note_to_domain(r) for r in rows]

    async def count_for_conversation(
        self, tenant_id: TenantId, conversation_id: uuid.UUID
    ) -> int:
        return int(
            (
                await self._s.execute(
                    select(func.count())
                    .select_from(m.WhatsAppConversationNoteModel)
                    .where(
                        m.WhatsAppConversationNoteModel.tenant_id == tenant_id,
                        m.WhatsAppConversationNoteModel.conversation_id == conversation_id,
                    )
                )
            ).scalar_one()
        )

    async def delete(self, tenant_id: TenantId, note_id: uuid.UUID) -> None:
        await self._s.execute(
            delete(m.WhatsAppConversationNoteModel).where(
                m.WhatsAppConversationNoteModel.id == note_id,
                m.WhatsAppConversationNoteModel.tenant_id == tenant_id,
            )
        )


class PostCallConfigRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, config: PostCallConfig) -> None:
        self._s.add(_post_call_config_to_row(config))

    async def get(self, tenant_id: TenantId, config_id: uuid.UUID) -> PostCallConfig | None:
        row = (
            await self._s.execute(
                select(m.PostCallConfigModel).where(
                    m.PostCallConfigModel.id == config_id,
                    m.PostCallConfigModel.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        return map_.post_call_config_to_domain(row) if row else None

    async def list_for_chatbot(
        self, tenant_id: TenantId, chatbot_id: ChatbotId
    ) -> list[PostCallConfig]:
        rows = (
            await self._s.execute(
                select(m.PostCallConfigModel)
                .where(
                    m.PostCallConfigModel.tenant_id == tenant_id,
                    m.PostCallConfigModel.chatbot_id == chatbot_id,
                )
                .order_by(m.PostCallConfigModel.created_at)
            )
        ).scalars()
        return [map_.post_call_config_to_domain(r) for r in rows]

    async def update(self, config: PostCallConfig) -> None:
        row = await self._s.get(m.PostCallConfigModel, config.id)
        if row is None:
            return
        row.delivery_method = config.delivery_method
        row.webhook_url = config.webhook_url
        row.email_to = config.email_to
        row.trigger_statuses = list(config.trigger_statuses)
        row.include_summary = config.include_summary
        row.include_transcript = config.include_transcript
        row.include_sentiment = config.include_sentiment
        row.include_extracted = config.include_extracted
        row.enabled = config.enabled

    async def delete(self, tenant_id: TenantId, config_id: uuid.UUID) -> None:
        row = (
            await self._s.execute(
                select(m.PostCallConfigModel).where(
                    m.PostCallConfigModel.id == config_id,
                    m.PostCallConfigModel.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            await self._s.delete(row)


def _post_call_config_to_row(c: PostCallConfig) -> m.PostCallConfigModel:
    return m.PostCallConfigModel(
        id=c.id,
        tenant_id=c.tenant_id,
        chatbot_id=c.chatbot_id,
        delivery_method=c.delivery_method,
        webhook_url=c.webhook_url,
        email_to=c.email_to,
        trigger_statuses=list(c.trigger_statuses),
        include_summary=c.include_summary,
        include_transcript=c.include_transcript,
        include_sentiment=c.include_sentiment,
        include_extracted=c.include_extracted,
        enabled=c.enabled,
        created_at=c.created_at,
    )


class PostCallDeliveryRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def claim(self, delivery: PostCallDelivery) -> bool:
        """Insert the delivery row, returning False when this (config, session)
        pair was already dispatched.

        This is the idempotency gate, enforced by the unique constraint rather
        than a read-then-write check — two concurrent "session ended" calls
        would both pass a read check and double-post to the customer's ATS.
        """
        stmt = (
            pg_insert(m.PostCallDeliveryModel)
            .values(
                id=delivery.id,
                tenant_id=delivery.tenant_id,
                chatbot_id=delivery.chatbot_id,
                config_id=delivery.config_id,
                session_id=delivery.session_id,
                call_status=delivery.call_status,
                delivery_method=delivery.delivery_method,
                destination=delivery.destination,
                status=delivery.status,
                error=delivery.error,
                payload=delivery.payload,
                created_at=delivery.created_at,
            )
            .on_conflict_do_nothing(constraint="uq_post_call_delivery_config_session")
            .returning(m.PostCallDeliveryModel.id)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none() is not None

    async def finish(self, delivery: PostCallDelivery) -> None:
        row = await self._s.get(m.PostCallDeliveryModel, delivery.id)
        if row is None:
            return
        row.status = delivery.status
        row.error = delivery.error
        row.payload = delivery.payload

    async def list_for_chatbot(
        self, tenant_id: TenantId, chatbot_id: ChatbotId, limit: int = 50
    ) -> list[PostCallDelivery]:
        rows = (
            await self._s.execute(
                select(m.PostCallDeliveryModel)
                .where(
                    m.PostCallDeliveryModel.tenant_id == tenant_id,
                    m.PostCallDeliveryModel.chatbot_id == chatbot_id,
                )
                .order_by(m.PostCallDeliveryModel.created_at.desc())
                .limit(limit)
            )
        ).scalars()
        return [map_.post_call_delivery_to_domain(r) for r in rows]


class BroadcastRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, broadcast: Broadcast) -> None:
        self._s.add(
            m.BroadcastModel(
                id=broadcast.id,
                tenant_id=broadcast.tenant_id,
                chatbot_id=broadcast.chatbot_id,
                whatsapp_channel_id=broadcast.whatsapp_channel_id,
                whatsapp_session_id=broadcast.whatsapp_session_id,
                sender_kind=broadcast.sender_kind,
                mode=broadcast.mode,
                name=broadcast.name,
                message_template=broadcast.message_template,
                status=broadcast.status,
                total_count=broadcast.total_count,
                sent_count=broadcast.sent_count,
                delivered_count=broadcast.delivered_count,
                read_count=broadcast.read_count,
                replied_count=broadcast.replied_count,
                failed_count=broadcast.failed_count,
                created_at=broadcast.created_at,
                updated_at=broadcast.updated_at,
            )
        )

    async def get(self, tenant_id: TenantId, broadcast_id: uuid.UUID) -> Broadcast | None:
        row = (
            await self._s.execute(
                select(m.BroadcastModel).where(
                    m.BroadcastModel.id == broadcast_id,
                    m.BroadcastModel.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        return map_.broadcast_to_domain(row) if row else None

    async def get_unscoped(self, broadcast_id: uuid.UUID) -> Broadcast | None:
        """Resolve without a tenant filter — used only by the Twilio status
        callback, which carries no tenant context (mirrors
        WhatsAppChannelRepository.get_by_phone_number)."""
        row = await self._s.get(m.BroadcastModel, broadcast_id)
        return map_.broadcast_to_domain(row) if row else None

    async def list_for_tenant(self, tenant_id: TenantId) -> list[Broadcast]:
        rows = (
            await self._s.execute(
                select(m.BroadcastModel)
                .where(m.BroadcastModel.tenant_id == tenant_id)
                .order_by(m.BroadcastModel.created_at.desc())
            )
        ).scalars()
        return [map_.broadcast_to_domain(r) for r in rows]

    async def list_active(self) -> list[Broadcast]:
        """Campaigns the send sweep should work on. Deliberately not
        tenant-scoped: the sweep runs as a background job across all tenants."""
        rows = (
            await self._s.execute(
                select(m.BroadcastModel).where(m.BroadcastModel.status == "sending")
            )
        ).scalars()
        return [map_.broadcast_to_domain(r) for r in rows]

    async def update(self, broadcast: Broadcast) -> None:
        row = await self._s.get(m.BroadcastModel, broadcast.id)
        if row is None:
            return
        row.name = broadcast.name
        row.message_template = broadcast.message_template
        row.status = broadcast.status
        row.total_count = broadcast.total_count
        row.sent_count = broadcast.sent_count
        row.delivered_count = broadcast.delivered_count
        row.read_count = broadcast.read_count
        row.replied_count = broadcast.replied_count
        row.failed_count = broadcast.failed_count
        row.updated_at = broadcast.updated_at

    async def delete(self, tenant_id: TenantId, broadcast_id: uuid.UUID) -> None:
        row = (
            await self._s.execute(
                select(m.BroadcastModel).where(
                    m.BroadcastModel.id == broadcast_id,
                    m.BroadcastModel.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            await self._s.delete(row)


class BroadcastRecipientRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add_many(self, recipients: list[BroadcastRecipient]) -> int:
        """Bulk-insert, skipping numbers already on this campaign. Returns how
        many rows were created so the UI can report the duplicate count."""
        if not recipients:
            return 0
        stmt = (
            pg_insert(m.BroadcastRecipientModel)
            .values(
                [
                    {
                        "id": r.id,
                        "broadcast_id": r.broadcast_id,
                        "tenant_id": r.tenant_id,
                        "phone_number": r.phone_number,
                        "display_name": r.display_name,
                        "status": r.status,
                        "error": r.error,
                        "provider_message_id": r.provider_message_id,
                        "session_id": r.session_id,
                        "attempts": r.attempts,
                        "created_at": r.created_at,
                        "updated_at": r.updated_at,
                    }
                    for r in recipients
                ]
            )
            .on_conflict_do_nothing(constraint="uq_broadcast_recipient_phone")
            .returning(m.BroadcastRecipientModel.id)
        )
        return len((await self._s.execute(stmt)).scalars().all())

    async def get(
        self, tenant_id: TenantId, recipient_id: uuid.UUID
    ) -> BroadcastRecipient | None:
        row = (
            await self._s.execute(
                select(m.BroadcastRecipientModel).where(
                    m.BroadcastRecipientModel.id == recipient_id,
                    m.BroadcastRecipientModel.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        return map_.broadcast_recipient_to_domain(row) if row else None

    async def get_by_provider_message_id(
        self, provider_message_id: str
    ) -> BroadcastRecipient | None:
        """Twilio status callbacks identify the recipient only by message SID
        and carry no tenant context — so this lookup is deliberately unscoped."""
        if not provider_message_id:
            return None
        row = (
            await self._s.execute(
                select(m.BroadcastRecipientModel).where(
                    m.BroadcastRecipientModel.provider_message_id == provider_message_id
                )
            )
        ).scalar_one_or_none()
        return map_.broadcast_recipient_to_domain(row) if row else None

    async def get_by_session(self, session_id: SessionId) -> BroadcastRecipient | None:
        """Used by the inbound webhook to flip a recipient to `replied` — the
        webhook knows the session it appended to, not the campaign."""
        row = (
            (
                await self._s.execute(
                    select(m.BroadcastRecipientModel).where(
                        m.BroadcastRecipientModel.session_id == session_id
                    )
                )
            )
            .scalars()
            .first()
        )
        return map_.broadcast_recipient_to_domain(row) if row else None

    async def list_for_broadcast(
        self,
        broadcast_id: uuid.UUID,
        *,
        status: str | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[BroadcastRecipient]:
        stmt = select(m.BroadcastRecipientModel).where(
            m.BroadcastRecipientModel.broadcast_id == broadcast_id
        )
        stmt = _recipient_filters(stmt, status, search)
        stmt = stmt.order_by(m.BroadcastRecipientModel.created_at)
        if limit is not None:
            stmt = stmt.limit(limit).offset(offset)
        rows = (await self._s.execute(stmt)).scalars()
        return [map_.broadcast_recipient_to_domain(r) for r in rows]

    async def count_for_broadcast(
        self, broadcast_id: uuid.UUID, *, status: str | None = None, search: str | None = None
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(m.BroadcastRecipientModel)
            .where(m.BroadcastRecipientModel.broadcast_id == broadcast_id)
        )
        stmt = _recipient_filters(stmt, status, search)
        return int((await self._s.execute(stmt)).scalar_one())

    async def claim_pending(
        self, broadcast_id: uuid.UUID, limit: int
    ) -> list[BroadcastRecipient]:
        """Take the next batch of unsent recipients, locking them for this worker.

        SKIP LOCKED is what lets the sweep run on more than one process (or be
        re-entered by a retried background task) without two workers messaging
        the same person twice.
        """
        rows = (
            await self._s.execute(
                select(m.BroadcastRecipientModel)
                .where(
                    m.BroadcastRecipientModel.broadcast_id == broadcast_id,
                    m.BroadcastRecipientModel.status == "pending",
                )
                .order_by(m.BroadcastRecipientModel.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
        return [map_.broadcast_recipient_to_domain(r) for r in rows]

    async def update(self, recipient: BroadcastRecipient) -> None:
        row = await self._s.get(m.BroadcastRecipientModel, recipient.id)
        if row is None:
            return
        row.display_name = recipient.display_name
        row.status = recipient.status
        row.error = recipient.error
        row.provider_message_id = recipient.provider_message_id
        row.session_id = recipient.session_id
        row.attempts = recipient.attempts
        row.updated_at = recipient.updated_at

    async def reset_failed(self, broadcast_id: uuid.UUID) -> int:
        """Requeue every failed recipient; returns how many were requeued."""
        result = await self._s.execute(
            update(m.BroadcastRecipientModel)
            .where(
                m.BroadcastRecipientModel.broadcast_id == broadcast_id,
                m.BroadcastRecipientModel.status == "failed",
            )
            .values(
                status="pending",
                error="",
                provider_message_id="",
                updated_at=datetime.now(UTC),
            )
        )
        return int(result.rowcount or 0)


def _recipient_filters(stmt, status: str | None, search: str | None):
    """Shared WHERE clauses for the list/count pair, so a filtered page and its
    total can never disagree about what they're filtering on."""
    if status:
        stmt = stmt.where(m.BroadcastRecipientModel.status == status)
    if search and search.strip():
        like = f"%{search.strip()}%"
        stmt = stmt.where(
            m.BroadcastRecipientModel.phone_number.ilike(like)
            | m.BroadcastRecipientModel.display_name.ilike(like)
        )
    return stmt


class TenantIntegrationRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def upsert(self, integration: TenantIntegration) -> None:
        """Connect or re-connect in one statement.

        Re-connecting is how a tenant rotates a leaked webhook URL, so this
        overwrites config rather than erroring on the (tenant, integration)
        unique constraint.
        """
        stmt = (
            pg_insert(m.TenantIntegrationModel)
            .values(
                id=integration.id,
                tenant_id=integration.tenant_id,
                integration_id=integration.integration_id,
                config=integration.config,
                enabled=integration.enabled,
                created_at=integration.created_at,
                updated_at=integration.updated_at,
            )
            .on_conflict_do_update(
                constraint="uq_tenant_integration",
                set_={
                    "config": integration.config,
                    "enabled": integration.enabled,
                    "updated_at": integration.updated_at,
                },
            )
        )
        await self._s.execute(stmt)

    async def get(self, tenant_id: TenantId, integration_id: str) -> TenantIntegration | None:
        row = (
            await self._s.execute(
                select(m.TenantIntegrationModel).where(
                    m.TenantIntegrationModel.tenant_id == tenant_id,
                    m.TenantIntegrationModel.integration_id == integration_id,
                )
            )
        ).scalar_one_or_none()
        return map_.tenant_integration_to_domain(row) if row else None

    async def list_for_tenant(self, tenant_id: TenantId) -> list[TenantIntegration]:
        rows = (
            await self._s.execute(
                select(m.TenantIntegrationModel)
                .where(m.TenantIntegrationModel.tenant_id == tenant_id)
                .order_by(m.TenantIntegrationModel.created_at)
            )
        ).scalars()
        return [map_.tenant_integration_to_domain(r) for r in rows]

    async def delete(self, tenant_id: TenantId, integration_id: str) -> None:
        await self._s.execute(
            delete(m.TenantIntegrationModel).where(
                m.TenantIntegrationModel.tenant_id == tenant_id,
                m.TenantIntegrationModel.integration_id == integration_id,
            )
        )


class IssueReportRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, report: IssueReport) -> None:
        self._s.add(
            m.IssueReportModel(
                id=report.id,
                tenant_id=report.tenant_id,
                name=report.name,
                email=report.email,
                phone=report.phone,
                report_type=report.report_type,
                priority=report.priority,
                subject=report.subject,
                description=report.description,
                status=report.status,
                page_url=report.page_url,
                user_agent=report.user_agent,
                email_sent=report.email_sent,
                created_at=report.created_at,
            )
        )

    async def get(self, tenant_id: TenantId, report_id: uuid.UUID) -> IssueReport | None:
        row = (
            await self._s.execute(
                select(m.IssueReportModel).where(
                    m.IssueReportModel.id == report_id,
                    m.IssueReportModel.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        return map_.issue_report_to_domain(row) if row else None

    async def list_for_tenant(self, tenant_id: TenantId, limit: int = 100) -> list[IssueReport]:
        rows = (
            await self._s.execute(
                select(m.IssueReportModel)
                .where(m.IssueReportModel.tenant_id == tenant_id)
                .order_by(m.IssueReportModel.created_at.desc())
                .limit(limit)
            )
        ).scalars()
        return [map_.issue_report_to_domain(r) for r in rows]

    async def mark_email_sent(self, report_id: uuid.UUID, sent: bool) -> None:
        row = await self._s.get(m.IssueReportModel, report_id)
        if row is not None:
            row.email_sent = sent


class VoiceProfileRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, profile: VoiceProfile) -> None:
        self._s.add(
            m.VoiceProfileModel(
                id=profile.id,
                tenant_id=profile.tenant_id,
                name=profile.name,
                gender=profile.gender,
                language=profile.language,
                description=profile.description,
                sample_storage_key=profile.sample_storage_key,
                sample_content_type=profile.sample_content_type,
                sample_bytes=profile.sample_bytes,
                duration_seconds=profile.duration_seconds,
                provider=profile.provider,
                provider_voice_id=profile.provider_voice_id,
                status=profile.status,
                error=profile.error,
                created_at=profile.created_at,
                updated_at=profile.updated_at,
            )
        )

    async def get(self, tenant_id: TenantId, profile_id: uuid.UUID) -> VoiceProfile | None:
        row = (
            await self._s.execute(
                select(m.VoiceProfileModel).where(
                    m.VoiceProfileModel.id == profile_id,
                    m.VoiceProfileModel.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        return map_.voice_profile_to_domain(row) if row else None

    async def list_for_tenant(self, tenant_id: TenantId) -> list[VoiceProfile]:
        rows = (
            await self._s.execute(
                select(m.VoiceProfileModel)
                .where(m.VoiceProfileModel.tenant_id == tenant_id)
                .order_by(m.VoiceProfileModel.created_at.desc())
            )
        ).scalars()
        return [map_.voice_profile_to_domain(r) for r in rows]

    async def update(self, profile: VoiceProfile) -> None:
        row = await self._s.get(m.VoiceProfileModel, profile.id)
        if row is None:
            return
        row.name = profile.name
        row.gender = profile.gender
        row.language = profile.language
        row.description = profile.description
        row.provider = profile.provider
        row.provider_voice_id = profile.provider_voice_id
        row.status = profile.status
        row.error = profile.error
        row.updated_at = profile.updated_at

    async def delete(self, tenant_id: TenantId, profile_id: uuid.UUID) -> None:
        await self._s.execute(
            delete(m.VoiceProfileModel).where(
                m.VoiceProfileModel.id == profile_id,
                m.VoiceProfileModel.tenant_id == tenant_id,
            )
        )


def _digits(phone_number: str) -> str:
    """A phone number reduced to what actually identifies it."""
    return "".join(c for c in phone_number if c.isdigit())


class WhatsAppWebSessionRepositoryImpl:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, ws: WhatsAppWebSession) -> None:
        self._s.add(
            m.WhatsAppWebSessionModel(
                id=ws.id,
                tenant_id=ws.tenant_id,
                chatbot_id=ws.chatbot_id,
                status=ws.status,
                phone_number=ws.phone_number,
                display_name=ws.display_name,
                qr_data_url=ws.qr_data_url,
                qr_expires_at=ws.qr_expires_at,
                last_error=ws.last_error,
                linked_at=ws.linked_at,
                last_seen_at=ws.last_seen_at,
                created_at=ws.created_at,
                updated_at=ws.updated_at,
            )
        )

    async def get(self, tenant_id: TenantId, session_id: uuid.UUID) -> WhatsAppWebSession | None:
        row = (
            await self._s.execute(
                select(m.WhatsAppWebSessionModel).where(
                    m.WhatsAppWebSessionModel.id == session_id,
                    m.WhatsAppWebSessionModel.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        return map_.whatsapp_web_session_to_domain(row) if row else None

    async def get_unscoped(self, session_id: uuid.UUID) -> WhatsAppWebSession | None:
        """Resolve without a tenant filter — only the bridge webhook uses this,
        and it carries no tenant context (mirrors the Twilio callbacks)."""
        row = await self._s.get(m.WhatsAppWebSessionModel, session_id)
        return map_.whatsapp_web_session_to_domain(row) if row else None

    async def list_for_tenant(self, tenant_id: TenantId) -> list[WhatsAppWebSession]:
        rows = (
            await self._s.execute(
                select(m.WhatsAppWebSessionModel)
                .where(m.WhatsAppWebSessionModel.tenant_id == tenant_id)
                .order_by(m.WhatsAppWebSessionModel.created_at.desc())
            )
        ).scalars()
        return [map_.whatsapp_web_session_to_domain(r) for r in rows]

    async def list_linked_to_number(
        self, tenant_id: TenantId, phone_number: str
    ) -> list[WhatsAppWebSession]:
        """Every session in this workspace that ended up on the same handset.

        Scanning in Python rather than filtering in SQL: WhatsApp reports the
        number in whatever shape the handset registered it, so "+971501234567",
        "971501234567" and "971 50 123 4567" are all the same phone and none of
        them are string-equal. A workspace is capped at five sessions, so the
        scan is over a handful of rows.
        """
        wanted = _digits(phone_number)
        if not wanted:
            return []
        return [
            ws
            for ws in await self.list_for_tenant(tenant_id)
            if _digits(ws.phone_number) == wanted
        ]

    async def list_linked_to_number_anywhere(
        self, phone_number: str
    ) -> list[WhatsAppWebSession]:
        wanted = _digits(phone_number)
        if not wanted:
            return []
        rows = (
            await self._s.execute(
                select(m.WhatsAppWebSessionModel).where(
                    m.WhatsAppWebSessionModel.status == "linked"
                )
            )
        ).scalars()
        sessions = [map_.whatsapp_web_session_to_domain(r) for r in rows]
        return [ws for ws in sessions if _digits(ws.phone_number) == wanted]

    async def update(self, ws: WhatsAppWebSession) -> None:
        row = await self._s.get(m.WhatsAppWebSessionModel, ws.id)
        if row is None:
            return
        row.chatbot_id = ws.chatbot_id
        row.status = ws.status
        row.phone_number = ws.phone_number
        row.display_name = ws.display_name
        row.qr_data_url = ws.qr_data_url
        row.qr_expires_at = ws.qr_expires_at
        row.last_error = ws.last_error
        row.linked_at = ws.linked_at
        row.last_seen_at = ws.last_seen_at
        row.updated_at = ws.updated_at

    async def delete(self, tenant_id: TenantId, session_id: uuid.UUID) -> None:
        await self._s.execute(
            delete(m.WhatsAppWebSessionModel).where(
                m.WhatsAppWebSessionModel.id == session_id,
                m.WhatsAppWebSessionModel.tenant_id == tenant_id,
            )
        )


class OnboardingRepositoryImpl:
    """Milestones (derived) and preferences (stored) for the staged shell.

    The milestone half is a read model: seven EXISTS probes across tables owned
    by other aggregates, answered in one round trip rather than by loading six
    collections into the browser and counting them there — which is what the
    dashboard used to do, on every single visit.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def milestones(self, tenant_id: TenantId) -> Milestones:
        # `EXISTS (...)` per question, all in one statement. Every one of these
        # is answerable from an index, and none of them needs a count — "is
        # there at least one" is the entire question.
        def exists(stmt) -> object:
            return select(stmt.exists()).scalar_subquery()

        bots = m.ChatbotModel
        # "Configured" deliberately excludes the pristine Default Assistant
        # that `provisioning.py` creates at signup: renamed, given a voice, or
        # published all count, and so does simply having a second one.
        configured = exists(
            select(bots.id).where(
                bots.tenant_id == tenant_id,
                or_(
                    bots.name != "Default Assistant",
                    bots.is_public.is_(True),
                    bots.voice_profile_id.isnot(None),
                ),
            )
        )
        second_bot = select(func.count(bots.id)).where(bots.tenant_id == tenant_id)

        knowledge = exists(
            select(m.DocumentModel.id).where(
                m.DocumentModel.tenant_id == tenant_id,
                m.DocumentModel.status == "ready",
            )
        )
        # Test mode opens a real chat session (see TestModePanel), so a session
        # existing at all is the signal — no separate client-side flag needed,
        # which is what made "tested" reset on every new browser before.
        tested = exists(
            select(m.ChatSessionModel.id).where(m.ChatSessionModel.tenant_id == tenant_id)
        )
        whatsapp = exists(
            select(m.WhatsAppChannelModel.id).where(
                m.WhatsAppChannelModel.tenant_id == tenant_id
            )
        )
        live = exists(
            select(bots.id).where(bots.tenant_id == tenant_id, bots.is_public.is_(True))
        )
        integrations = exists(
            select(m.TenantIntegrationModel.id).where(
                m.TenantIntegrationModel.tenant_id == tenant_id,
                m.TenantIntegrationModel.enabled.is_(True),
            )
        )

        row = (
            await self._s.execute(
                select(
                    configured.label("configured"),
                    second_bot.label("bot_count"),
                    knowledge.label("knowledge"),
                    tested.label("tested"),
                    whatsapp.label("whatsapp"),
                    live.label("live"),
                    integrations.label("integrations"),
                )
            )
        ).one()

        return Milestones(
            assistant_configured=bool(row.configured) or (row.bot_count or 0) > 1,
            knowledge_ready=bool(row.knowledge),
            assistant_tested=bool(row.tested),
            channel_connected=bool(row.whatsapp),
            assistant_live=bool(row.live),
            # Filled in by the caller: booking readiness is a domain
            # computation over hours and eligibility, not a row check, and it
            # already has an owner in the scheduling repositories.
            appointments_ready=False,
            integrations_connected=bool(row.integrations),
        )

    async def get_prefs(self, tenant_id: TenantId, user_id: UserId) -> OnboardingPrefs | None:
        row = (
            await self._s.execute(
                select(m.OnboardingPrefsModel).where(
                    m.OnboardingPrefsModel.user_id == user_id,
                    m.OnboardingPrefsModel.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return OnboardingPrefs(
            user_id=UserId(row.user_id),
            tenant_id=TenantId(row.tenant_id),
            nav_mode=row.nav_mode,  # type: ignore[arg-type]
            tours_completed=list(row.tours_completed or []),
            dismissed=list(row.dismissed or []),
            celebrated_stages=list(row.celebrated_stages or []),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def save_prefs(self, prefs: OnboardingPrefs) -> None:
        """Upsert. The first write for a user is also the row's creation —
        there is no separate "start onboarding" moment to hang an INSERT on."""
        now = datetime.now(UTC)
        stmt = (
            pg_insert(m.OnboardingPrefsModel)
            .values(
                user_id=prefs.user_id,
                tenant_id=prefs.tenant_id,
                nav_mode=prefs.nav_mode,
                tours_completed=list(prefs.tours_completed),
                dismissed=list(prefs.dismissed),
                celebrated_stages=list(prefs.celebrated_stages),
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[m.OnboardingPrefsModel.user_id],
                set_={
                    "nav_mode": prefs.nav_mode,
                    "tours_completed": list(prefs.tours_completed),
                    "dismissed": list(prefs.dismissed),
                    "celebrated_stages": list(prefs.celebrated_stages),
                    "updated_at": now,
                },
            )
        )
        await self._s.execute(stmt)
