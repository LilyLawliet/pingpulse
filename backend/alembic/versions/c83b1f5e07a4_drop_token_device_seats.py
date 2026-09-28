"""Drop the seat system: a licence is a token and a date

Seats counted machines. The intent was to stop a token being forwarded around,
and it did not: the device id lives in the client's own storage and clearing it
mints a new one. What it did do was refuse the client themselves. Opening the
dashboard in a second browser, on a new laptop, or after clearing site data
spent a seat that was never released, so a licence filled up with machines
nobody was using and the owner was locked out of their own shop.

It cost the truth on screen as well. A refused machine got a 403 on every call,
and the dashboard could not tell that apart from a business that had not been
set up - so it drew a setup checklist for a shop that had been running for
weeks, and told them to create a business they already owned.

So: a licence is the token and the date it expires. Expiry is enforced in
`resolve_token` and always was.

One way. The rows were bookkeeping about machines, not anything a customer
gave us, and there is nothing to restore them from; downgrade rebuilds the
structure empty so an older revision can still run.

Revision ID: c83b1f5e07a4
Revises: a71f4c2e9d05
Create Date: 2026-09-28
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c83b1f5e07a4"
down_revision: str | None = "a71f4c2e9d05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_token_devices_token", table_name="token_devices")
    op.drop_table("token_devices")
    op.drop_column("access_tokens", "max_devices")


def downgrade() -> None:
    op.add_column(
        "access_tokens",
        sa.Column("max_devices", sa.Integer(), server_default="3", nullable=False),
    )
    op.create_table(
        "token_devices",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token", sa.String(length=128), nullable=False),
        sa.Column("device_id", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["token"], ["access_tokens.token"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token", "device_id", name="uq_token_device"),
    )
    op.create_index("ix_token_devices_token", "token_devices", ["token"])
