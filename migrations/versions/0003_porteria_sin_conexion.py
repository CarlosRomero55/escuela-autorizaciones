"""Portería sin conexión: marca en las recogidas registradas sin red.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("recogidas") as batch:
        batch.add_column(sa.Column("sin_conexion", sa.Boolean(), server_default=sa.text("false"), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("recogidas") as batch:
        batch.drop_column("sin_conexion")
