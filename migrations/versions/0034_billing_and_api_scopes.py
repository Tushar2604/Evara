"""Make the plan the thing that decides what a workspace can do

Until now every workspace was identical: the same document ceiling, the same
token quota, and an `api_keys` table that any account could have written to had
there been an endpoint for it. There wasn't one — keys could be authenticated
but never created — so the programmatic surface existed and was unreachable.
That is the hole this migration fills, and it fills it in the shape the product
actually sells:

  * `subscriptions` — one row per paying tenant, absent for free ones. Absence
    *is* the free tier, so signing up writes no billing row and there is no
    backfill here for the tenants that already exist.
  * `billing_transactions` — append-only history, the "Billing history" tab.
  * `api_usage_daily` — calls per tenant per day, the meter behind the monthly
    allowance. Daily buckets, not per-request rows: the allowance is the only
    consumer, and it wants a SUM over ~30 rows, not over a million.
  * `api_keys.scopes` / `.plan_tier` — what the plan granted when the key was
    minted. Existing rows (if any) get `[]` and `free`, which is the safe
    reading: a key from before scopes existed grants nothing until reissued.

The scope columns are additive and nullable-by-default, so this is safe to run
ahead of the deploy that uses them.

Revision ID: 0034_billing_and_api_scopes
Revises: 0033_onboarding_prefs
Create Date: 2026-09-07
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0034_billing_and_api_scopes"
down_revision: str | None = "0033_onboarding_prefs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# All three new tables are tenant-scoped, so they take the same RLS policy as
# everything else (dormant until the app connects as a non-owner role — see
# 0001 for the caveat).
_RLS_TABLES = ("subscriptions", "billing_transactions", "api_usage_daily")


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        # Unique, not just indexed: "what is this workspace paying for" must
        # have exactly one answer, and a duplicate row is the kind of bug that
        # shows up as a customer intermittently losing a feature.
        sa.Column(
            "tenant_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("tier", sa.String(20), nullable=False, server_default="free"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        # NULL = never lapses. Only the free tier should carry that.
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auto_renew", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_subscriptions_tenant_id", "subscriptions", ["tenant_id"])

    op.create_table(
        "billing_transactions",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("amount_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("description", sa.String(255), nullable=False, server_default=""),
        sa.Column("plan_tier", sa.String(20), nullable=True),
        sa.Column("reference", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_billing_transactions_tenant_id", "billing_transactions", ["tenant_id"])
    op.create_index("ix_billing_transactions_kind", "billing_transactions", ["kind"])

    op.create_table(
        "api_usage_daily",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False, server_default="0"),
        # The constraint the increment depends on: metering is an upsert on
        # (tenant, day), and concurrent API calls race without it.
        sa.UniqueConstraint("tenant_id", "day", name="uq_api_usage_tenant_day"),
    )
    op.create_index("ix_api_usage_daily_tenant_id", "api_usage_daily", ["tenant_id"])
    op.create_index("ix_api_usage_daily_day", "api_usage_daily", ["day"])

    op.add_column(
        "api_keys",
        sa.Column(
            "scopes",
            pg.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "api_keys",
        sa.Column("plan_tier", sa.String(20), nullable=False, server_default="free"),
    )
    op.add_column("api_keys", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("api_keys", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))

    for table in _RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING "
            f"(tenant_id = current_setting('app.tenant_id', true)::uuid)"
        )


def downgrade() -> None:
    for table in _RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")

    op.drop_column("api_keys", "revoked_at")
    op.drop_column("api_keys", "last_used_at")
    op.drop_column("api_keys", "plan_tier")
    op.drop_column("api_keys", "scopes")

    op.drop_index("ix_api_usage_daily_day", table_name="api_usage_daily")
    op.drop_index("ix_api_usage_daily_tenant_id", table_name="api_usage_daily")
    op.drop_table("api_usage_daily")

    op.drop_index("ix_billing_transactions_kind", table_name="billing_transactions")
    op.drop_index("ix_billing_transactions_tenant_id", table_name="billing_transactions")
    op.drop_table("billing_transactions")

    op.drop_index("ix_subscriptions_tenant_id", table_name="subscriptions")
    op.drop_table("subscriptions")
