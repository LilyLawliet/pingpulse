"""A secret per organization, so a phone can subscribe to the diary.

An iCalendar client fetches its URL for years with no way to be prompted for
anything, so the URL has to carry the secret. Nullable: a tenant that never
asks for a feed never gets one, and a null is the difference between "not set
up" and "set up and empty".

Unique, because the token is what resolves the request to a tenant. Postgres
allows repeated NULLs under a unique constraint, which is what lets every
organization that has not asked for one coexist.

Revision ID: a71f4c2e9d05
Revises: e2c7f1a93b40
"""

from alembic import op
import sqlalchemy as sa

revision = "a71f4c2e9d05"
down_revision = "e2c7f1a93b40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("calendar_token", sa.String(length=64), nullable=True),
    )
    op.create_unique_constraint(
        "uq_organizations_calendar_token", "organizations", ["calendar_token"]
    )
    op.create_index(
        "ix_organizations_calendar_token",
        "organizations",
        ["calendar_token"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_organizations_calendar_token", table_name="organizations")
    op.drop_constraint(
        "uq_organizations_calendar_token", "organizations", type_="unique"
    )
    op.drop_column("organizations", "calendar_token")
