"""Appointments: the record a booking never had.

The agent confirmed an appointment for 1am on a date the customer never chose,
and when she asked for it to be cancelled it said it had been. Neither claim
could be checked, because there was nothing in the database that meant "this
person is coming at this time" - so nothing could contradict the first
statement and nothing could be cancelled to make the second one true.

Nothing is backfilled. There is no honest way to invent appointments for
conversations that discussed them, and a table seeded with guesses would be
worse than an empty one: it would look like history.

Revision ID: d1a4e7f28c53
Revises: c9f3a61d8b42
Create Date: 2026-09-19
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d1a4e7f28c53"
down_revision = "c9f3a61d8b42"
branch_labels = None
depends_on = None


def _uuid_type():
    """UUID on PostgreSQL, a 36-character string everywhere else.

    The test suite runs on SQLite, which has no native UUID, and a migration
    that only works on one of the two is a migration that gets discovered in
    production.
    """
    return postgresql.UUID(as_uuid=True) if _is_postgres() else sa.String(36)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "appointments",
        sa.Column("id", _uuid_type(), primary_key=True),
        sa.Column(
            "organization_id",
            _uuid_type(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "contact_id",
            _uuid_type(),
            sa.ForeignKey("crm_contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "timezone_name",
            sa.String(64),
            nullable=False,
            server_default="UTC",
        ),
        sa.Column("kind", sa.String(16), nullable=False, server_default="onsite"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("location", sa.String(300)),
        sa.Column("notes", sa.Text()),
        sa.Column("source", sa.String(16), nullable=False, server_default="agent"),
        sa.Column("failure_reason", sa.String(300)),
        sa.Column(
            "replaces_id",
            _uuid_type(),
            sa.ForeignKey("appointments.id", ondelete="SET NULL"),
        ),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if _is_postgres() else sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    op.create_index("ix_appointments_organization_id", "appointments", ["organization_id"])
    op.create_index("ix_appointments_contact_id", "appointments", ["contact_id"])
    op.create_index("ix_appointments_org_start", "appointments", ["organization_id", "starts_at"])
    op.create_index("ix_appointments_contact", "appointments", ["contact_id", "starts_at"])

    # Two customers cannot hold the same slot. Enforced by the database rather
    # than by a check in application code, because the check and the insert
    # are two statements and two requests can sit between them - which is
    # exactly the "two customers requesting the same appointment slot" case.
    #
    # Only live rows take part: a cancelled appointment must not block the
    # time it used to occupy from being sold to somebody else.
    if _is_postgres():
        op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        op.execute(
            """
            ALTER TABLE appointments
            ADD CONSTRAINT no_double_booking
            EXCLUDE USING gist (
                organization_id WITH =,
                tstzrange(starts_at, ends_at) WITH &&
            )
            WHERE (status = 'confirmed')
            """
        )


def downgrade() -> None:
    if _is_postgres():
        op.execute("ALTER TABLE appointments DROP CONSTRAINT IF EXISTS no_double_booking")
    op.drop_index("ix_appointments_contact", table_name="appointments")
    op.drop_index("ix_appointments_org_start", table_name="appointments")
    op.drop_index("ix_appointments_contact_id", table_name="appointments")
    op.drop_index("ix_appointments_organization_id", table_name="appointments")
    op.drop_table("appointments")
