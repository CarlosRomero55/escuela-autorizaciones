import json
from typing import Any

from sqlalchemy.orm import Session

from app.models import BitacoraAuditoria


def registrar(
    db: Session,
    *,
    usuario_id: int | None,
    accion: str,
    entidad: str,
    entidad_id: int | None = None,
    cambio: dict[str, Any] | None = None,
    ip: str | None = None,
    rol: str | None = None,
    motivo: str | None = None,
) -> None:
    """Agrega una entrada a la bitácora dentro de la misma transacción de la acción auditada.

    Así, si la acción falla, tampoco queda registrada como realizada. Nunca se guardan
    documentos, teléfonos ni correos en `cambio`.
    """
    db.add(
        BitacoraAuditoria(
            usuario_id=usuario_id,
            rol=rol,
            accion=accion,
            entidad=entidad,
            entidad_id=entidad_id,
            cambio=json.dumps(cambio, ensure_ascii=False, default=str) if cambio is not None else None,
            motivo=motivo,
            ip=ip,
        )
    )
