"""Esquema inicial: tablas del modelo y bitácora inmutable.

Revision ID: 0001
Revises:
Create Date: 2026-10-29

Nota: la creación de tablas usa el metadata de app.models en el momento de esta migración.
Si el modelo cambia en el futuro, las migraciones siguientes deben usar operaciones explícitas
(op.add_column, op.create_index, etc.) en lugar de volver a llamar a create_all.
"""

from alembic import op

from app.models import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)

    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION bitacora_inmutable() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'bitacora_auditoria es inmutable: no se permite %', TG_OP;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_bitacora_inmutable
            BEFORE UPDATE OR DELETE ON bitacora_auditoria
            FOR EACH ROW EXECUTE FUNCTION bitacora_inmutable();
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_bitacora_inmutable ON bitacora_auditoria;")
        op.execute("DROP FUNCTION IF EXISTS bitacora_inmutable();")
    Base.metadata.drop_all(bind=bind)
