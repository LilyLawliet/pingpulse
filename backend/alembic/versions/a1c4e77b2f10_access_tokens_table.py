"""access_tokens: database-backed token authentication

Replaces email/password login. A client is issued an opaque token which is
checked against this table on every request, so revoking one takes effect
immediately instead of when it would have expired.

`user_id` is what makes tenancy work: tenant-scoped queries resolve the
caller's organization through their membership, so the token needs an identity
to act as. It is nullable so a token can exist before it is assigned.

Revision ID: a1c4e77b2f10
Revises: 634c85aa3faa
Create Date: 2026-09-10
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a1c4e77b2f10"
down_revision: str | None = "634c85aa3faa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "access_tokens",
        # The raw token is the primary key: one indexed lookup per request.
        sa.Column("token", sa.String(length=128), nullable=False),
        sa.Column("client_name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token"),
    )
    # Expiry is filtered on every validation; user_id is joined on to resolve
    # the caller's organization.
    op.create_index("ix_access_tokens_expires_at", "access_tokens", ["expires_at"])
    op.create_index("ix_access_tokens_user_id", "access_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_access_tokens_user_id", table_name="access_tokens")
    op.drop_index("ix_access_tokens_expires_at", table_name="access_tokens")
    op.drop_table("access_tokens")
