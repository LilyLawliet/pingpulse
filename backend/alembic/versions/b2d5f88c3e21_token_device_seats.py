"""token_devices: seat limiting so a licence is not passed around

A licence covers one person and their team. Each machine that uses a token
claims a seat, and once `max_devices` are claimed a new machine is refused.

This is a licence control, not a security boundary — a determined user can
clear their local device id. It stops the casual case: a token forwarded to
five colleagues, or resold.

Revision ID: b2d5f88c3e21
Revises: a1c4e77b2f10
Create Date: 2026-09-10
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b2d5f88c3e21"
down_revision: str | None = "a1c4e77b2f10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing tokens keep working and get the default allowance.
    op.add_column(
        "access_tokens",
        sa.Column(
            "max_devices", sa.Integer(), server_default="3", nullable=False
        ),
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
        # One row per machine per token: re-opening the app must not consume
        # another seat.
        sa.UniqueConstraint("token", "device_id", name="uq_token_device"),
    )
    op.create_index("ix_token_devices_token", "token_devices", ["token"])


def downgrade() -> None:
    op.drop_index("ix_token_devices_token", table_name="token_devices")
    op.drop_table("token_devices")
    op.drop_column("access_tokens", "max_devices")
