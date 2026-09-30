"""Learned answers: the shop's own replies to what the agent couldn't answer

Revision ID: f1c4a8d2e6b3
Revises: e3b7d2a14c90
Create Date: 2026-09-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f1c4a8d2e6b3"
down_revision: str | None = "e3b7d2a14c90"
branch_labels = None
depends_on = None


def _uuid_type():
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.UUID(as_uuid=True)
    return sa.String(36)


def upgrade() -> None:
    now = sa.text("now()") if op.get_bind().dialect.name == "postgresql" else sa.text("CURRENT_TIMESTAMP")
    op.create_table(
        "learned_answers",
        sa.Column("id", _uuid_type(), primary_key=True),
        sa.Column(
            "organization_id",
            _uuid_type(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("contact_id", _uuid_type(), sa.ForeignKey("crm_contacts.id", ondelete="SET NULL")),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text()),
        sa.Column("status", sa.String(16), nullable=False, server_default="waiting"),
        sa.Column("knowledge_id", _uuid_type()),
        sa.Column("asked_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
        sa.Column("taught_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_learned_answers_organization_id", "learned_answers", ["organization_id"])
    op.create_index("ix_learned_org_status", "learned_answers", ["organization_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_learned_org_status", table_name="learned_answers")
    op.drop_index("ix_learned_answers_organization_id", table_name="learned_answers")
    op.drop_table("learned_answers")
