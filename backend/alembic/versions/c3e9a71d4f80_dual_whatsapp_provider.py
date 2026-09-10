"""channel_configs: dual WhatsApp provider (Twilio or paired QR session)

A tenant either sends through Twilio's API or through a WhatsApp Web session
paired by scanning a QR code. Both write to the same tables, so switching
provider changes only the transport — no conversation, contact or pipeline
stage moves or disappears.

Existing rows default to TWILIO, which is what they already were.

Revision ID: c3e9a71d4f80
Revises: b2d5f88c3e21
Create Date: 2026-09-10
"""

from alembic import op
import sqlalchemy as sa


revision: str = "c3e9a71d4f80"
down_revision: str | None = "b2d5f88c3e21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A plain string rather than a native enum: adding a provider later is then
    # a code change, not a migration that locks the table.
    op.add_column(
        "channel_configs",
        sa.Column(
            "whatsapp_provider",
            sa.String(length=20),
            server_default="TWILIO",
            nullable=False,
        ),
    )
    # QR_SESSION only — where the pairing has got to, and when it last
    # connected. Null for Twilio channels.
    op.add_column(
        "channel_configs",
        sa.Column("session_status", sa.String(length=24), nullable=True),
    )
    op.add_column(
        "channel_configs",
        sa.Column("session_connected_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("channel_configs", "session_connected_at")
    op.drop_column("channel_configs", "session_status")
    op.drop_column("channel_configs", "whatsapp_provider")
