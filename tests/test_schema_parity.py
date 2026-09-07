"""Guard: every ORM column on the new tables must exist in a migration.

The realistic failure mode when adding a table in two places is a column that
exists on the model but was never migrated — the app then works locally against
a `create_all` database and 500s in production on the first query. This diffs
the Alembic offline DDL against the ORM metadata so that mismatch fails CI
rather than a deploy.
"""

from __future__ import annotations

import io
import pathlib
import re

import pytest
from alembic import command
from alembic.config import Config
from src.infrastructure.persistence import models as m

# Tables introduced by 0012/0013/0015, plus the columns added to `chatbots`.
NEW_TABLES = {
    "post_call_configs": m.PostCallConfigModel,
    "post_call_deliveries": m.PostCallDeliveryModel,
    "broadcasts": m.BroadcastModel,
    "broadcast_recipients": m.BroadcastRecipientModel,
    "tenant_integrations": m.TenantIntegrationModel,
    "issue_reports": m.IssueReportModel,
    "voice_profiles": m.VoiceProfileModel,
    "whatsapp_web_sessions": m.WhatsAppWebSessionModel,
    "oauth_connections": m.OAuthConnectionModel,
    "whatsapp_conversation_notes": m.WhatsAppConversationNoteModel,
    # Extended by 0022/0024/0026 long after 0009 created it — exactly the
    # shape of drift this file exists to catch.
    "whatsapp_conversations": m.WhatsAppConversationModel,
    # Billing (0034). The plan is what gates assistants and API keys, so a
    # column that exists only on the model here means a 500 the first time
    # somebody opens the Billing page in production.
    "subscriptions": m.SubscriptionModel,
    "billing_transactions": m.BillingTransactionModel,
    "api_usage_daily": m.ApiUsageDailyModel,
}


# Rendered as explicit ranges rather than <base>:head, because data migrations
# issue real SELECTs and can't run offline (0004 backfills publishable keys,
# 0014 backfills the prompt). These are the ranges carrying the DDL this file
# checks, skipping 0014 in between.
DDL_RANGES = (
    ("0008_interviews", "0013_broadcasts"),
    ("0014_tighten_default_prompt", "0026_whatsapp_crm_inbox"),
    ("0033_onboarding_prefs", "0034_billing_and_api_scopes"),
)


@pytest.fixture(scope="module")
def migration_sql() -> str:
    """Offline DDL for revisions 0012 and 0013 — no database required."""
    parts = []
    for start, end in DDL_RANGES:
        buffer = io.StringIO()
        config = Config("alembic.ini")
        config.attributes["configure_logger"] = False
        # `output_buffer` (not `stdout`) is what offline mode writes DDL to.
        config.output_buffer = buffer
        command.upgrade(config, f"{start}:{end}", sql=True)
        parts.append(buffer.getvalue())
    sql = "\n".join(parts)
    assert sql.strip(), "offline migration render produced no SQL"
    return sql


def _migrated_columns(sql: str, table: str) -> set[str]:
    """Every column a migration gives `table`, however it was added.

    A column can arrive two ways — in the original CREATE, or in a later
    ALTER ... ADD COLUMN — and a check that only reads the CREATE reports a
    perfectly-migrated column as missing the first time a table is extended.
    """
    match = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", sql, re.S)
    assert match, f"{table} is never created by a migration"
    constraints = ("PRIMARY KEY", "FOREIGN KEY", "UNIQUE", "CONSTRAINT", "CHECK")
    columns = set()
    for line in match.group(1).splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith(constraints):
            continue
        columns.add(line.split()[0])

    columns.update(re.findall(rf"ALTER TABLE {table} ADD COLUMN (\w+)", sql))
    return columns


@pytest.mark.parametrize("table", sorted(NEW_TABLES))
def test_every_orm_column_is_migrated(migration_sql: str, table: str) -> None:
    model = NEW_TABLES[table]
    orm_columns = {c.name for c in model.__table__.columns}
    missing = orm_columns - _migrated_columns(migration_sql, table)
    assert not missing, f"{table} model has columns no migration creates: {sorted(missing)}"


@pytest.mark.parametrize("table", sorted(NEW_TABLES))
def test_no_migrated_column_is_missing_from_the_orm(migration_sql: str, table: str) -> None:
    model = NEW_TABLES[table]
    orm_columns = {c.name for c in model.__table__.columns}
    orphaned = _migrated_columns(migration_sql, table) - orm_columns
    assert not orphaned, f"{table} has migrated columns the ORM never reads: {sorted(orphaned)}"


def test_flow_sections_column_is_added_to_chatbots(migration_sql: str) -> None:
    assert "flow_sections" in {c.name for c in m.ChatbotModel.__table__.columns}
    assert re.search(r"ALTER TABLE chatbots ADD COLUMN flow_sections", migration_sql)


def test_flow_sections_backfills_existing_rows(migration_sql: str) -> None:
    # Without a server default, every pre-existing chatbot row would read NULL
    # and the mapper would show an empty flow for bots that never opted out.
    assert re.search(
        r"ADD COLUMN flow_sections JSONB DEFAULT '\[\]'::jsonb NOT NULL", migration_sql
    )


def test_assistant_config_column_is_added_to_chatbots(migration_sql: str) -> None:
    assert "assistant_config" in {c.name for c in m.ChatbotModel.__table__.columns}
    assert re.search(r"ALTER TABLE chatbots ADD COLUMN assistant_config", migration_sql)


def test_assistant_config_backfills_existing_rows(migration_sql: str) -> None:
    # Same failure as flow_sections: without a server default every existing
    # assistant reads NULL and loses its language, voice, and model settings.
    assert re.search(
        r"ADD COLUMN assistant_config JSONB DEFAULT '\{.*?\}'::jsonb NOT NULL",
        migration_sql,
        re.S,
    )


def test_google_oauth_connections_are_carried_into_the_generic_table(
    migration_sql: str,
) -> None:
    # These rows are consent a user already gave. Dropping the old table
    # without copying them would silently break interview scheduling until
    # every tenant reconnected.
    assert "INSERT INTO oauth_connections" in migration_sql
    assert "FROM google_oauth_connections" in migration_sql
    assert migration_sql.index("INSERT INTO oauth_connections") < migration_sql.index(
        "DROP TABLE google_oauth_connections"
    )


def test_dedupe_and_idempotency_constraints_exist(migration_sql: str) -> None:
    # Both are load-bearing: the first stops a double post-call dispatch, the
    # second stops a re-uploaded contact list double-messaging everyone.
    assert "uq_post_call_delivery_config_session UNIQUE (config_id, session_id)" in migration_sql
    assert "uq_broadcast_recipient_phone UNIQUE (broadcast_id, phone_number)" in migration_sql


def test_new_tenant_scoped_tables_have_rls(migration_sql: str) -> None:
    for table in NEW_TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in migration_sql
        assert f"CREATE POLICY tenant_isolation ON {table}" in migration_sql


def test_status_callback_lookup_column_is_indexed(migration_sql: str) -> None:
    # Twilio callbacks arrive keyed only on the message SID; without this index
    # every callback is a sequential scan of the recipients table.
    assert re.search(
        r"CREATE INDEX ix_broadcast_recipients_provider_message_id", migration_sql
    )


def test_send_sweep_query_is_indexed(migration_sql: str) -> None:
    assert "ix_broadcast_recipients_broadcast_status" in migration_sql


def test_one_integration_connection_per_tenant(migration_sql: str) -> None:
    # Also the ON CONFLICT target that makes re-connecting an upsert instead of
    # a duplicate-key error.
    assert "uq_tenant_integration UNIQUE (tenant_id, integration_id)" in migration_sql


def test_voice_column_is_added_to_chatbots(migration_sql: str) -> None:
    assert "voice_profile_id" in {c.name for c in m.ChatbotModel.__table__.columns}
    assert re.search(r"ALTER TABLE chatbots ADD COLUMN voice_profile_id", migration_sql)


def test_deleting_a_voice_does_not_delete_the_assistant(migration_sql: str) -> None:
    # ON DELETE SET NULL: removing a voice must degrade the assistant to the
    # default voice, never cascade into deleting the assistant itself.
    match = re.search(
        r"ALTER TABLE chatbots ADD COLUMN voice_profile_id.*?voice_profiles \(id\)([^;]*);",
        migration_sql,
        re.S,
    )
    assert match, "the voice_profile_id FK is missing"
    assert "ON DELETE SET NULL" in match.group(1)


def test_whatsapp_auth_state_is_persisted_in_postgres(migration_sql: str) -> None:
    # Baileys defaults to on-disk auth. The container filesystem is ephemeral on
    # free hosts, so keys must live in Postgres or every sleep would force the
    # user to re-scan a QR.
    assert "CREATE TABLE whatsapp_web_auth" in migration_sql
    assert re.search(r"PRIMARY KEY \(session_id, key\)", migration_sql)


def test_deleting_an_assistant_does_not_unlink_whatsapp(migration_sql: str) -> None:
    # SET NULL, not CASCADE: removing an assistant must leave the user's linked
    # WhatsApp account intact, just no longer auto-replying.
    match = re.search(
        r"CREATE TABLE whatsapp_web_sessions \((.*?)\n\);", migration_sql, re.S
    )
    assert match, "whatsapp_web_sessions is never created"
    fk = re.search(
        r"FOREIGN KEY\(chatbot_id\) REFERENCES chatbots \(id\)([^,\n]*)", match.group(1)
    )
    assert fk and "ON DELETE SET NULL" in fk.group(1)


@pytest.fixture(scope="module")
def migration_source() -> str:
    path = (
        pathlib.Path(__file__).parent.parent
        / "migrations"
        / "versions"
        / "0019_pin_assistant_knowledge.py"
    )
    return path.read_text(encoding="utf-8")


class TestKnowledgeBackfillMigration:
    """0019 changes what an empty `allowed_document_ids` means.

    The migration is what stops that change from silently stripping the
    knowledge from every assistant built under the old rule, so its two guards
    are worth pinning: it must only touch already-empty lists, and it must pin
    only documents that can actually answer.
    """

    def test_only_assistants_with_no_documents_are_touched(self, migration_source: str) -> None:
        # An assistant already scoped to specific documents is an explicit
        # choice; overwriting it would destroy real configuration.
        assert "WHERE c.allowed_document_ids = '[]'::jsonb" in migration_source

    def test_only_ready_documents_are_pinned(self, migration_source: str) -> None:
        # A failed or still-parsing document has no chunks, so pinning it would
        # scope an assistant to something unreadable.
        assert "d.status = 'ready'" in migration_source

    def test_an_empty_match_yields_a_list_not_null(self, migration_source: str) -> None:
        # jsonb_agg returns NULL over zero rows; NULL reads back as "unset".
        assert "COALESCE" in migration_source
        assert "'[]'::jsonb" in migration_source
