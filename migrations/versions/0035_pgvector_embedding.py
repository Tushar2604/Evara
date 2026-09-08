"""document_chunks.embedding: float[] -> pgvector, with an ANN index

Retrieval ran `ChunkRepositoryImpl.search` by loading *every* chunk row for a
tenant (text + embedding array) into Python on every single chat/WhatsApp
turn and scoring cosine similarity in a loop — the module docstring already
claimed "pgvector-backed vector store," but the column was a plain
`float[]`, so no index could ever apply to it. As a tenant's document count
grows this is the dominant cost of every chat request: linear scan, full
row transfer, and it holds a pooled DB connection for the whole scan+score
duration. This migration makes the docstring true.

`float8[]` casts directly to `vector` (pgvector ships that cast), so this is
a straight `ALTER COLUMN ... TYPE ... USING` — no data needs to be
regenerated or re-embedded. An HNSW index (not IVFFlat) is used because it
needs no training/list-count tuning and performs well from the very first
row, which matters for a bootstrapped deployment where most tenants have a
handful of documents, not the tens of thousands IVFFlat is tuned for.

Revision ID: 0035_pgvector_embedding
Revises: 0034_billing_and_api_scopes
Create Date: 2026-09-08
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from src.config import get_settings

revision: str = "0035_pgvector_embedding"
down_revision: str | None = "0034_billing_and_api_scopes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = get_settings().embedding_dim


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        f"ALTER TABLE document_chunks "
        f"ALTER COLUMN embedding TYPE vector({EMBEDDING_DIM}) "
        f"USING embedding::vector({EMBEDDING_DIM})"
    )
    # Cosine ops to match the cosine-similarity scoring the app already does
    # (`1 - cosine_distance`). HNSW builds incrementally, so this is safe to
    # run before any chunks exist and needs no post-load REINDEX/ANALYZE step
    # the way IVFFlat would.
    op.execute(
        "CREATE INDEX ix_document_chunks_embedding_hnsw ON document_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding_hnsw")
    op.execute(
        "ALTER TABLE document_chunks "
        "ALTER COLUMN embedding TYPE double precision[] USING embedding::double precision[]"
    )
