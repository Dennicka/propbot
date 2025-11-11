"""init
Revision ID: 0001_init
Revises:
Create Date: 2025-10-15

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "config_changes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ts", sa.String(32), nullable=False),
        sa.Column("op", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("token", sa.String(64), nullable=True),
        sa.Column("blob", sa.Text, nullable=True),
    )


def downgrade():
    op.drop_table("config_changes")
