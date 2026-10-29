from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import UsuarioStaff

SESION_REQUERIDA = "Inicie sesión para continuar."


def get_current_user(request: Request, db: Session = Depends(get_db)) -> UsuarioStaff:
    uid = request.session.get("uid")
    if uid is None:
        raise HTTPException(status_code=401, detail=SESION_REQUERIDA)
    usuario = db.get(UsuarioStaff, uid)
    if usuario is None or not usuario.activo:
        request.session.clear()
        raise HTTPException(status_code=401, detail=SESION_REQUERIDA)
    return usuario


def require_roles(*roles: str):
    """Dependencia que verifica el rol en el backend. La interfaz no basta para proteger un recurso."""

    def _check(usuario: UsuarioStaff = Depends(get_current_user)) -> UsuarioStaff:
        if usuario.rol not in roles:
            raise HTTPException(status_code=403, detail="No tiene permiso para esta acción.")
        return usuario

    return _check
