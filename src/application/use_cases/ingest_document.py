"""Document ingestion — a resumable pipeline.

Runs as an in-process background task. Each step persists progress so that if
the host sleeps mid-job (common on free tiers), `resume_pending` can pick the
document back up from its last completed state instead of restarting.
"""

from __future__ import annotations

import structlog

from src.application.ports.repositories import UnitOfWork
from src.application.ports.services import Chunker, DocumentParser, Embedder, LLMProvider, ObjectStorage
from src.domain.document.entities import Chunk, Document, IngestionStatus
from src.domain.document.events import DocumentIngested, DocumentIngestionFailed
from src.domain.shared.identifiers import DocumentId, TenantId

log = structlog.get_logger(__name__)

# Embed in batches to stay within free-tier request limits.
_EMBED_BATCH = 64


class IngestDocument:
    def __init__(
        self,
        uow: UnitOfWork,
        storage: ObjectStorage,
        parser: DocumentParser,
        chunker: Chunker,
        embedder: Embedder,
        llm: LLMProvider,
    ) -> None:
        self._uow = uow
        self._storage = storage
        self._parser = parser
        self._chunker = chunker
        self._embedder = embedder
        self._llm = llm

    async def execute(self, tenant_id: TenantId, document_id: DocumentId) -> None:
        # IMPORTANT: no DB connection is held across the parse/describe-image/
        # embedding calls below. Free-tier Postgres caps connections
        # aggressively, and a single large document can spend minutes in
        # parsing + embedding — holding a pooled connection for that whole
        # span would starve ordinary chat/API traffic of the same pool. Each
        # step below opens its own short transaction (see `_save`), mirroring
        # `AskChatbot`. This also means progress genuinely survives a
        # mid-pipeline restart, matching this module's docstring.
        async with self._uow as uow:
            uow.set_tenant_scope(tenant_id)
            doc = await uow.documents.get(tenant_id, document_id)
            if doc is None:
                log.warning("ingest.document_missing", document_id=str(document_id))
                return
            if doc.status in (IngestionStatus.READY, IngestionStatus.PENDING):
                # PENDING means upload not completed yet; READY means done.
                return

        try:
            await self._run_pipeline(tenant_id, doc)
        except Exception as exc:  # noqa: BLE001 - convert to domain failure state
            log.exception("ingest.failed", document_id=str(document_id))
            doc.mark_failed(str(exc)[:500])
            async with self._uow as uow:
                uow.set_tenant_scope(tenant_id)
                await uow.documents.update(doc)
                uow.collect_event(
                    DocumentIngestionFailed(
                        tenant_id=tenant_id, document_id=doc.id, reason=str(exc)[:500]
                    )
                )
                await uow.commit()

    async def _save(self, tenant_id: TenantId, doc: Document) -> None:
        """Persist the document's current state in its own short transaction."""
        async with self._uow as uow:
            uow.set_tenant_scope(tenant_id)
            await uow.documents.update(doc)
            await uow.commit()

    async def _run_pipeline(self, tenant_id: TenantId, doc: Document) -> None:
        # 1. PARSE
        if doc.status == IngestionStatus.UPLOADED:
            doc.transition_to(IngestionStatus.PARSING)
            await self._save(tenant_id, doc)
        raw = await self._storage.get_bytes(doc.storage_key)
        if doc.content_type.startswith("image/"):
            # No OCR dependency in this stack — a vision-capable LLM
            # transcribes the image into text, which then flows through the
            # exact same chunk/embed pipeline as any other document.
            text = await self._llm.describe_image(raw, doc.content_type)
        else:
            text = await self._parser.extract_text(raw, doc.content_type, doc.filename)

        # 2. CHUNK
        doc.transition_to(IngestionStatus.CHUNKING)
        await self._save(tenant_id, doc)
        pieces = self._chunker.chunk(text)
        if not pieces:
            raise ValueError("No extractable text found in document.")

        chunks = [
            Chunk(
                tenant_id=doc.tenant_id,
                document_id=doc.id,
                ordinal=i,
                text=piece,
                token_estimate=max(1, len(piece) // 4),
            )
            for i, piece in enumerate(pieces)
        ]

        # 3. EMBED + UPSERT (replace any partial prior run to stay idempotent)
        doc.transition_to(IngestionStatus.EMBEDDING)
        await self._save(tenant_id, doc)
        async with self._uow as uow:
            uow.set_tenant_scope(tenant_id)
            await uow.chunks.delete_for_document(doc.tenant_id, doc.id)
            await uow.commit()
        for start in range(0, len(chunks), _EMBED_BATCH):
            batch = chunks[start : start + _EMBED_BATCH]
            vectors = await self._embedder.embed_documents([c.text for c in batch])
            async with self._uow as uow:
                uow.set_tenant_scope(tenant_id)
                await uow.chunks.add_many(batch, vectors)
                await uow.commit()

        # 4. FINALIZE
        doc.mark_ready(chunk_count=len(chunks))
        async with self._uow as uow:
            uow.set_tenant_scope(tenant_id)
            await uow.documents.update(doc)
            uow.collect_event(
                DocumentIngested(
                    tenant_id=doc.tenant_id, document_id=doc.id, chunk_count=len(chunks)
                )
            )
            await uow.commit()
        log.info("ingest.ready", document_id=str(doc.id), chunks=len(chunks))


class ResumePendingIngestions:
    """Startup sweep: re-enqueue any document left mid-pipeline by a restart."""

    def __init__(self, uow: UnitOfWork, ingest: IngestDocument) -> None:
        self._uow = uow
        self._ingest = ingest

    async def execute(self) -> int:
        async with self._uow as uow:
            stuck = await uow.documents.list_resumable()
        for doc in stuck:
            log.info("ingest.resume", document_id=str(doc.id), status=doc.status)
            await self._ingest.execute(doc.tenant_id, doc.id)
        return len(stuck)
