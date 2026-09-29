"""Blocked-out time: an appointment with no customer

"I'm busy Thursday afternoon" had nowhere to go: every appointment belonged to
a contact. A block is now a confirmed appointment of kind "blocked" with no
contact, so the diary and the no_double_booking constraint treat it as taken.

Revision ID: d4a8e61f2c57
Revises: b7e2c4d91a36
Create Date: 2026-09-29
"""

from alembic import op


revision: str = "d4a8e61f2c57"
down_revision: str | None = "b7e2c4d91a36"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("appointments", "contact_id", nullable=True)


def downgrade() -> None:
    # Blocks cannot exist without a customer column that allows none.
    op.execute("DELETE FROM appointments WHERE contact_id IS NULL")
    op.alter_column("appointments", "contact_id", nullable=False)
