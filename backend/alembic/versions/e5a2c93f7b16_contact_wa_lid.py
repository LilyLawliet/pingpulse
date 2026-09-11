"""Give a contact its WhatsApp LID a column of its own.

WhatsApp increasingly addresses a chat by LID — a privacy identifier like
153231615328393@lid — rather than by phone number, and only sometimes attaches
the real number to the message. With nowhere to record the LID, the identifier
itself was stored as the contact's phone number: the shop saw a fifteen-digit
number that cannot be dialled, and the same person appeared a second time the
moment they switched the account on their handset.

A column rather than a key in the metadata blob because this is an identity
that gets matched on for every inbound message, so it wants an index; and
because a JSON path predicate is written differently on PostgreSQL and SQLite,
which would mean the tests and production running different queries.

Nothing is backfilled. A LID cannot be told from a phone number by looking at
it — both are digits, and E.164 allows fifteen of them — so guessing here would
risk rewriting a real customer's number. Existing rows are reconciled by the
webhook the next time WhatsApp tells us who they are.

Revision ID: e5a2c93f7b16
Revises: d4f1b82e5a93
"""

from alembic import op
import sqlalchemy as sa

revision = "e5a2c93f7b16"
down_revision = "d4f1b82e5a93"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("crm_contacts", sa.Column("wa_lid", sa.String(length=32), nullable=True))
    op.create_index("ix_crm_contacts_wa_lid", "crm_contacts", ["wa_lid"])


def downgrade() -> None:
    op.drop_index("ix_crm_contacts_wa_lid", table_name="crm_contacts")
    op.drop_column("crm_contacts", "wa_lid")
