"""A QR channel has no number until the phone is scanned.

Pairing by QR asked the operator to type the number they were about to scan.
The scan reports it anyway - the bridge reads it off the handset the moment
the session authenticates - so the typed value was a second, unverified copy
of a fact the system already had, and it was the copy the uniqueness rule was
enforced against.

That copy is where the production fault came from: one handset typed as
"+923097209908" for one organization and "923097209908" for another. Neither
spelling collided, both rows were accepted, and the clash only surfaced when
the phone paired and the bridge wrote back the bare form onto a constraint
that refused it - inside a callback nobody was watching.

So the column becomes nullable and a QR channel is created without one. The
number arrives from the scan, in one spelling, from the handset itself.

Nothing is backfilled and nothing is cleared: every existing row keeps the
number it has. Postgres allows repeated NULLs in a unique constraint, so
several organizations can each have a pairing in flight without colliding on
"not yet known" - which is not a number and must not behave like one.

Revision ID: e2c7f1a93b40
Revises: d1a4e7f28c53
Create Date: 2026-09-22
"""

from alembic import op
import sqlalchemy as sa

revision = "e2c7f1a93b40"
down_revision = "d1a4e7f28c53"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "channel_configs",
        "phone_number",
        existing_type=sa.String(length=50),
        nullable=True,
    )


def downgrade() -> None:
    # A row with no number cannot satisfy NOT NULL, and inventing one would
    # put a fake number in front of a uniqueness rule. Pairings still waiting
    # on a scan are dropped instead: they carry no conversation history, and
    # re-pairing is one scan.
    op.execute("DELETE FROM channel_configs WHERE phone_number IS NULL")
    op.alter_column(
        "channel_configs",
        "phone_number",
        existing_type=sa.String(length=50),
        nullable=False,
    )
