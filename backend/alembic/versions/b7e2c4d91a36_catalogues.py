"""Catalogues: what each uploaded file sells, read once and checked

The price list used to be re-read from prose on every message by a growing set
of pattern rules. It is now read once, when the file is uploaded - by the
model, into structured products and rules, each checked against the file's own
text - and stored here for the owner to review and correct.

Revision ID: b7e2c4d91a36
Revises: c83b1f5e07a4
Create Date: 2026-09-29
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b7e2c4d91a36"
down_revision: str | None = "c83b1f5e07a4"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _uuid_type():
    return postgresql.UUID(as_uuid=True) if _is_postgres() else sa.String(36)


def upgrade() -> None:
    op.create_table(
        "catalogues",
        sa.Column("id", _uuid_type(), primary_key=True),
        sa.Column(
            "organization_id",
            _uuid_type(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(255), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("rules", sa.JSON(), nullable=False),
        sa.Column("dropped", sa.JSON(), nullable=False),
        sa.Column("read_by", sa.String(40), nullable=False, server_default="reader"),
        sa.Column("status", sa.String(16), nullable=False, server_default="read"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()") if _is_postgres() else sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_catalogues_organization_id", "catalogues", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_catalogues_organization_id", table_name="catalogues")
    op.drop_table("catalogues")
