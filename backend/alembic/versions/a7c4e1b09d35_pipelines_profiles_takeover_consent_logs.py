"""Per-tenant pipelines, rich lead profiles, human takeover, consent and logs.

The schema half of the client feedback round. Everything here is additive, and
every added column has a default, because there is a live client on this
database whose agent must keep answering while it runs.

The one part that is not additive is the board. It grew from four stages to
nine, and the stage a contact stands in is a string that has to still mean
something afterwards - so existing contacts are moved explicitly rather than
left pointing at a column that no longer exists. LEAD becomes NEW_LEAD,
DEMO_BOOKED becomes ESTIMATE_SCHEDULED, CLOSED becomes WON. Anything already
holding a new-style key is left alone, which makes the upgrade safe to run
twice.

Every existing organization is seeded with the nine default stages so their
board is populated the moment this lands. Code still falls back to the
constant when an organization has no rows, because the test suite builds its
schema with create_all and never runs this file.

Revision ID: a7c4e1b09d35
Revises: f6b3da04c827
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# The existing tables carry native uuid columns on PostgreSQL (the GUID
# decorator in models.py resolves to that there and to CHAR(36) only on the
# SQLite the tests use). A foreign key declared as a string would not match
# them, so every key here is declared the same way the earlier migrations
# declare theirs.
UUID = postgresql.UUID(as_uuid=True)

revision = "a7c4e1b09d35"
down_revision = "f6b3da04c827"
branch_labels = None
depends_on = None

# Kept as a literal rather than imported from app.models: a migration has to
# describe the schema as it was at this revision, and an import would silently
# change what this file does the next time somebody edits the constant.
DEFAULT_PIPELINE = (
    ("NEW_LEAD", "New lead", "slate", None),
    ("CONTACTED", "Contacted", "sky", None),
    ("QUALIFIED", "Qualified", "cyan", None),
    ("ESTIMATE_SCHEDULED", "Estimate scheduled", "violet", None),
    ("ESTIMATE_SENT", "Estimate sent", "amber", None),
    ("FOLLOW_UP", "Follow-up", "orange", None),
    ("WON", "Won", "emerald", "won"),
    ("LOST", "Lost", "rose", "lost"),
    ("UNQUALIFIED", "Unqualified", "zinc", "unqualified"),
)

LEGACY_PIPELINE = {
    "LEAD": "NEW_LEAD",
    "DEMO_BOOKED": "ESTIMATE_SCHEDULED",
    "CLOSED": "WON",
    # QUALIFIED keeps its key and needs no row here.
}


def upgrade() -> None:
    # ------------------------------------------------------------ new tables
    op.create_table(
        "tenant_pipelines",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "organization_id",
            UUID,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(40), nullable=False),
        sa.Column("label", sa.String(60), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("colour", sa.String(16), nullable=False, server_default="slate"),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("is_entry", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("organization_id", "key", name="uq_pipeline_org_key"),
    )
    op.create_index("ix_tenant_pipelines_organization_id", "tenant_pipelines", ["organization_id"])

    op.create_table(
        "audit_logs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "organization_id",
            UUID,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("resource_type", sa.String(40), nullable=True),
        sa.Column("resource_id", sa.String(64), nullable=True),
        sa.Column("changes", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_audit_logs_organization_id", "audit_logs", ["organization_id"])
    op.create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"])

    op.create_table(
        "system_errors",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "organization_id",
            UUID,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_system_errors_organization_id", "system_errors", ["organization_id"])
    op.create_index("ix_system_errors_category", "system_errors", ["category"])

    # -------------------------------------------------------- organizations
    op.add_column(
        "organizations",
        sa.Column("agent_config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column(
        "organizations",
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
    )

    # --------------------------------------------------------- crm_contacts
    for column in (
        sa.Column("company", sa.String(255), nullable=True),
        sa.Column("service_requested", sa.String(255), nullable=True),
        sa.Column("project_address", sa.Text(), nullable=True),
        sa.Column("budget", sa.String(120), nullable=True),
        sa.Column("timeline", sa.String(120), nullable=True),
        sa.Column("source", sa.String(80), nullable=True),
        sa.Column("photo_urls", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("custom_fields", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("qualification", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("ai_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opt_out", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("opt_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=True),
    ):
        op.add_column("crm_contacts", column)

    op.add_column(
        "crm_contacts",
        sa.Column(
            "assigned_to", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
    )
    op.create_index("ix_crm_contacts_assigned_to", "crm_contacts", ["assigned_to"])

    # ------------------------------------------------- seed and move the board
    connection = op.get_bind()
    organizations = connection.execute(sa.text("SELECT id FROM organizations")).fetchall()

    for (organization_id,) in organizations:
        for index, (key, label, colour, outcome) in enumerate(DEFAULT_PIPELINE):
            connection.execute(
                sa.text(
                    "INSERT INTO tenant_pipelines "
                    "(id, organization_id, key, label, order_index, colour, outcome, is_entry) "
                    "VALUES (:id, :org, :key, :label, :idx, :colour, :outcome, :entry)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "org": organization_id,
                    "key": key,
                    "label": label,
                    "idx": index,
                    "colour": colour,
                    "outcome": outcome,
                    "entry": index == 0,
                },
            )

    # Move everybody standing in an old column. Contacts already on a new key
    # match nothing here, so running this twice changes nothing the second time.
    for old_key, new_key in LEGACY_PIPELINE.items():
        connection.execute(
            sa.text(
                "UPDATE crm_contacts SET pipeline_stage = :new WHERE pipeline_stage = :old"
            ),
            {"new": new_key, "old": old_key},
        )

    # New contacts land in the first column from here on.
    op.alter_column("crm_contacts", "pipeline_stage", server_default="NEW_LEAD")


def downgrade() -> None:
    op.alter_column("crm_contacts", "pipeline_stage", server_default="LEAD")
    connection = op.get_bind()
    for old_key, new_key in LEGACY_PIPELINE.items():
        connection.execute(
            sa.text("UPDATE crm_contacts SET pipeline_stage = :old WHERE pipeline_stage = :new"),
            {"new": new_key, "old": old_key},
        )

    op.drop_index("ix_crm_contacts_assigned_to", "crm_contacts")
    for name in (
        "assigned_to", "consent_at", "opt_out_at", "opt_out", "last_read_at", "ai_enabled",
        "summary", "qualification", "custom_fields", "photo_urls", "source", "timeline",
        "budget", "project_address", "service_requested", "company",
    ):
        op.drop_column("crm_contacts", name)

    op.drop_column("organizations", "timezone")
    op.drop_column("organizations", "agent_config")

    op.drop_table("system_errors")
    op.drop_table("audit_logs")
    op.drop_table("tenant_pipelines")
