"""Dependencias de la API JSON: sesión vigente, actor con rol activo y control de rol."""

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import sesiones
from app.db import get_db
from app.models import Usuario
from app.permisos import Actor, construir_actor

SESION_REQUERIDA = "Inicie sesión para continuar."


def get_actor(request: Request, db: Session = Depends(get_db)) -> Actor:
    sesion = sesiones.buscar_vigente(db, request.app.state.settings, request)
    if sesion is None or sesion.estado != "completa":
        raise HTTPException(status_code=401, detail=SESION_REQUERIDA)
    actor = construir_actor(sesion.usuario, sesion.rol_activo)
    if actor is None:
        raise HTTPException(status_code=403, detail="Su usuario no tiene roles vigentes.")
    request.state.actor = actor
    return actor


def get_current_user(actor: Actor = Depends(get_actor)) -> Usuario:
    return actor.usuario


def require_roles(*roles: str):
    """Verifica el rol activo en el backend. La interfaz no basta para proteger un recurso."""

    def _check(actor: Actor = Depends(get_actor)) -> Usuario:
        if actor.rol not in roles:
            raise HTTPException(status_code=403, detail="No tiene permiso para esta acción.")
        return actor.usuario

    return _check
