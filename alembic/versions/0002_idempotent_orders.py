"""idempotent orders tables"""

from alembic import op
import sqlalchemy as sa


revision = "0002_idempotent_orders"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "order_intents",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("intent_id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("account", sa.String(length=64), nullable=False),
        sa.Column("venue", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("tif", sa.String(length=16), nullable=True),
        sa.Column("qty", sa.Float, nullable=False),
        sa.Column("price", sa.Float, nullable=True),
        sa.Column("strategy", sa.String(length=64), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("broker_order_id", sa.String(length=128), nullable=True),
        sa.Column("replaced_by", sa.String(length=64), nullable=True),
        sa.Column("created_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_ts", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account", "venue", "intent_id", name="uq_order_intent_scope"),
    )
    op.create_index(
        "ix_order_intents_broker_order_id",
        "order_intents",
        ["broker_order_id"],
    )

    op.create_table(
        "cancel_intents",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("intent_id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("broker_order_id", sa.String(length=128), nullable=False),
        sa.Column("account", sa.String(length=64), nullable=False),
        sa.Column("venue", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("created_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_ts", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account", "venue", "intent_id", name="uq_cancel_intent_scope"),
    )
    op.create_index(
        "ix_cancel_intents_broker_order_id",
        "cancel_intents",
        ["broker_order_id"],
    )


def downgrade():
    op.drop_index("ix_cancel_intents_broker_order_id", table_name="cancel_intents")
    op.drop_table("cancel_intents")
    op.drop_index("ix_order_intents_broker_order_id", table_name="order_intents")
    op.drop_table("order_intents")

