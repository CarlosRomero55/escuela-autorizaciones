from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import security
from app.audit import registrar
from app.db import get_db
from app.deps import get_current_user
from app.models import UsuarioStaff
from app.schemas import LoginIn, TotpIn

router = APIRouter(prefix="/api/auth", tags=["autenticación"])

CREDENCIALES_INVALIDAS = "Correo o contraseña no válidos."
# Hash de referencia: se verifica aunque el usuario no exista, para que el tiempo de respuesta sea similar.
_HASH_REFERENCIA = security.hash_password("contraseña-de-referencia")


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    limiter = request.app.state.login_limiter
    clave = f"login:{_ip(request)}:{email}"
    if not limiter.allow(clave):
        raise HTTPException(status_code=429, detail="Demasiados intentos. Espere unos minutos e intente de nuevo.")

    usuario = db.scalar(select(UsuarioStaff).where(UsuarioStaff.email == email))
    if usuario is None:
        security.verify_password(_HASH_REFERENCIA, body.password)
        raise HTTPException(status_code=401, detail=CREDENCIALES_INVALIDAS)
    if not usuario.activo or not security.verify_password(usuario.password_hash, body.password):
        raise HTTPException(status_code=401, detail=CREDENCIALES_INVALIDAS)

    limiter.reset(clave)
    request.session.clear()
    if usuario.totp_secret:
        # El acceso queda pendiente hasta validar el segundo factor
        request.session["pendiente_uid"] = usuario.id
        return {"paso": "totp"}

    request.session["uid"] = usuario.id
    registrar(db, usuario_id=usuario.id, accion="inicio_sesion", entidad="sesion", ip=_ip(request))
    db.commit()
    return {"paso": "completo", "rol": usuario.rol}


@router.post("/totp")
def verificar_totp(body: TotpIn, request: Request, db: Session = Depends(get_db)):
    uid = request.session.get("pendiente_uid")
    if uid is None:
        raise HTTPException(status_code=401, detail="Inicie sesión primero.")

    limiter = request.app.state.totp_limiter
    if not limiter.allow(f"totp:{uid}"):
        request.session.clear()
        raise HTTPException(status_code=429, detail="Demasiados intentos. Inicie sesión de nuevo más tarde.")

    usuario = db.get(UsuarioStaff, uid)
    if usuario is None or not usuario.totp_secret or not security.verify_totp(usuario.totp_secret, body.codigo):
        raise HTTPException(status_code=401, detail="Código no válido.")

    limiter.reset(f"totp:{uid}")
    request.session.clear()
    request.session["uid"] = usuario.id
    registrar(db, usuario_id=usuario.id, accion="inicio_sesion", entidad="sesion", ip=_ip(request))
    db.commit()
    return {"paso": "completo", "rol": usuario.rol}


@router.post("/logout")
def cerrar_sesion(request: Request):
    request.session.clear()
    return {"paso": "cerrada"}


@router.get("/yo")
def quien_soy(usuario: UsuarioStaff = Depends(get_current_user)):
    return {"nombre": usuario.nombre, "rol": usuario.rol}
