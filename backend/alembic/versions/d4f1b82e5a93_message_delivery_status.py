"""Give every message an explicit delivery status.

A null `twilio_sid` used to mean "this did not go out", and that covered two
very different situations: a provider that refused the message, and a transport
that was momentarily missing and will be retried. The dashboard could only
show one of them, so a reply waiting in the retry queue looked identical to one
that had been lost.

Backfill reads the existing rows the way the old code did — inbound messages
arrived, so they are SENT; an outbound row with a sid was delivered; an
outbound row without one had failed, and nothing was going to retry it.

Revision ID: d4f1b82e5a93
Revises: c3e9a71d4f80
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4f1b82e5a93"
down_revision = "c3e9a71d4f80"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "delivery_status",
            sa.String(length=16),
            nullable=False,
            server_default="SENT",
        ),
    )
    op.execute(
        """
        UPDATE messages
           SET delivery_status = 'FAILED'
         WHERE sender <> 'user'
           AND twilio_sid IS NULL
        """
    )
    # Queued work is looked up by status, and on a busy tenant that is a small
    # slice of a large table.
    op.create_index(
        "ix_messages_delivery_status", "messages", ["delivery_status"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_messages_delivery_status", table_name="messages")
    op.drop_column("messages", "delivery_status")
