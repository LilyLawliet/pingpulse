"""Orders: a customer's yes to a worked-out summary, written down

Nothing recorded an order. The agent could quote a notebook, a delivery charge
and a total, and then say "I've noted it" about a row that did not exist.

Revision ID: a91c5e3f7d20
Revises: d4a8e61f2c57
Create Date: 2026-09-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a91c5e3f7d20"
down_revision: str | None = "d4a8e61f2c57"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _uuid_type():
    return postgresql.UUID(as_uuid=True) if _is_postgres() else sa.String(36)


def upgrade() -> None:
    now = sa.text("now()") if _is_postgres() else sa.text("CURRENT_TIMESTAMP")
    op.create_table(
        "orders",
        sa.Column("id", _uuid_type(), primary_key=True),
        sa.Column(
            "organization_id",
            _uuid_type(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "contact_id",
            _uuid_type(),
            sa.ForeignKey("crm_contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="placed"),
        sa.Column("lines", sa.JSON(), nullable=False),
        sa.Column("currency", sa.String(8)),
        sa.Column("goods_total", sa.Numeric(14, 2), nullable=False),
        sa.Column("discount", sa.Numeric(14, 2)),
        sa.Column("delivery_fee", sa.Numeric(14, 2)),
        sa.Column("total", sa.Numeric(14, 2), nullable=False),
        sa.Column("delivery_place", sa.String(120)),
        sa.Column("address", sa.Text()),
        sa.Column("payment_method", sa.String(60)),
        sa.Column("payment_status", sa.String(16), nullable=False, server_default="unpaid"),
        sa.Column("customer_note", sa.Text()),
        sa.Column("source", sa.String(16), nullable=False, server_default="agent"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("organization_id", "number", name="uq_order_org_number"),
    )
    op.create_index("ix_orders_organization_id", "orders", ["organization_id"])
    op.create_index("ix_orders_contact_id", "orders", ["contact_id"])
    op.create_index("ix_orders_org_created", "orders", ["organization_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_orders_org_created", table_name="orders")
    op.drop_index("ix_orders_contact_id", table_name="orders")
    op.drop_index("ix_orders_organization_id", table_name="orders")
    op.drop_table("orders")
