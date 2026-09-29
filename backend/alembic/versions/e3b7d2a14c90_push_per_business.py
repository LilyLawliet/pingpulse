"""A browser can be alerted for each business it said yes to

push_subscriptions was unique by endpoint, so the same browser opening a
second business moved its only row there, and the first business's alerts
stopped arriving. Unique by (organization, endpoint) now.

Revision ID: e3b7d2a14c90
Revises: a91c5e3f7d20
Create Date: 2026-09-30
"""

from alembic import op


revision: str = "e3b7d2a14c90"
down_revision: str | None = "a91c5e3f7d20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("push_subscriptions") as batch:
        batch.drop_constraint("uq_push_endpoint", type_="unique")
        batch.create_unique_constraint("uq_push_org_endpoint", ["organization_id", "endpoint"])


def downgrade() -> None:
    # Keep one row per endpoint - the newest - before it may be unique again.
    #
    # Ordered by id as well as time. created_at defaults to now(), which is
    # the same instant for every row written in one transaction: a browser
    # that subscribed to two businesses together had two rows neither of which
    # was older, so nothing was deleted and the index could not be rebuilt.
    # Rehearsed on a copy of production, where this downgrade failed with
    # "could not create unique index uq_push_endpoint" and left the table
    # with no unique constraint at all.
    op.execute(
        """
        DELETE FROM push_subscriptions a
        USING push_subscriptions b
        WHERE a.endpoint = b.endpoint
          AND (a.created_at, a.id) < (b.created_at, b.id)
        """
    )
    with op.batch_alter_table("push_subscriptions") as batch:
        batch.drop_constraint("uq_push_org_endpoint", type_="unique")
        batch.create_unique_constraint("uq_push_endpoint", ["endpoint"])
