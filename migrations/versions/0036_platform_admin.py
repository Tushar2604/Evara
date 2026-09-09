"""Platform-wide Super Admin role

Every role that has existed so far — `owner`, `admin`, `member` — is scoped to
one tenant; there has been no way to grant someone visibility across the whole
platform. This adds exactly that, as a flag orthogonal to the tenant role
rather than a new value inside it, because a platform admin is not "an admin
of a bigger tenant" — they see every tenant while still (or not) holding some
ordinary role inside their own.

`last_login_at` rides along: the Super Admin panel wants a cheap proxy for
"how active is this account" and a per-request heartbeat write is not worth
adding to the hot path for it, so a timestamp set once per login is the
approximation.

Revision ID: 0036_platform_admin
Revises: 0035_pgvector_embedding
Create Date: 2026-09-09
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_platform_admin"
down_revision: str | None = "0035_pgvector_embedding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_platform_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "users",
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "is_platform_admin")
