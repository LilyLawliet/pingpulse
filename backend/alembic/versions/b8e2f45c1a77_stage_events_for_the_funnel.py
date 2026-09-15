"""Stage events, so the funnel has a history to read.

The board only ever held where a lead *is*. That answers the question the
inbox asks and none of the questions a funnel asks: how many leads got as far
as qualified, where people stop, how long a win takes. The moment a lead moves,
the previous answer is gone - so every move now gets a row.

Nothing is backfilled, deliberately. Contacts that existed before this have no
history, and writing invented timestamps for their journeys would put movement
on a chart that never happened. The funnel reads a contact's current stage as
well as its events, so an untracked lead still counts where it stands; only
the timing of how it got there is unknown, rather than wrong.

Purely additive: one new table and two indexes. Nothing existing is touched,
so the live client's agent keeps answering while this runs.

Revision ID: b8e2f45c1a77
Revises: a7c4e1b09d35
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Native uuid on PostgreSQL, the same way every earlier migration declares it.
# A key declared as a string would not match the columns it points at.
UUID = postgresql.UUID(as_uuid=True)

revision = "b8e2f45c1a77"
down_revision = "a7c4e1b09d35"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stage_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "organization_id",
            UUID,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "contact_id",
            UUID,
            sa.ForeignKey("crm_contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Null is allowed because a lead can be recorded as arriving somewhere
        # from nowhere. Only real transitions are written today.
        sa.Column("from_stage", sa.String(50), nullable=True),
        sa.Column("to_stage", sa.String(50), nullable=False),
        sa.Column(
            "source", sa.String(16), nullable=False, server_default="agent"
        ),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_stage_events_organization_id", "stage_events", ["organization_id"])
    op.create_index("ix_stage_events_contact_id", "stage_events", ["contact_id"])
    # The shape every analytics query has: one tenant, one span of time.
    op.create_index("ix_stage_events_org_at", "stage_events", ["organization_id", "at"])

    # The same shape, for the two tables the analytics screen also reads. The
    # organization index alone still scans a tenant's whole history to answer
    # "the last seven days", which is the only question this screen asks.
    op.create_index("ix_contacts_org_created", "crm_contacts", ["organization_id", "created_at"])
    op.create_index("ix_messages_org_created", "messages", ["organization_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_messages_org_created", table_name="messages")
    op.drop_index("ix_contacts_org_created", table_name="crm_contacts")
    op.drop_index("ix_stage_events_org_at", table_name="stage_events")
    op.drop_index("ix_stage_events_contact_id", table_name="stage_events")
    op.drop_index("ix_stage_events_organization_id", table_name="stage_events")
    op.drop_table("stage_events")
