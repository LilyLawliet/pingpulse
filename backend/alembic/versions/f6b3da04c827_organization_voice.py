"""A shop's own writing voice, learned from its own replies.

Three nullable columns with a default, so every tenant already running keeps
the agent's current voice until somebody at that shop looks at a draft and
approves it. Nothing about an existing conversation changes on deploy.

Revision ID: f6b3da04c827
Revises: e5a2c93f7b16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f6b3da04c827"
down_revision = "e5a2c93f7b16"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("voice_style", sa.Text(), nullable=True))
    op.add_column(
        "organizations",
        sa.Column(
            "voice_examples",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column(
        "organizations",
        sa.Column("voice_learned_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organizations", "voice_learned_at")
    op.drop_column("organizations", "voice_examples")
    op.drop_column("organizations", "voice_style")
