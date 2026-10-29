"""Reglas de acceso: credenciales, bloqueo temporal, segundo factor y enlaces de cuenta.

La API JSON (`/api/auth`) y los formularios HTML usan exactamente estas funciones.
"""

import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app import crypto, security, sesiones
from app.audit import registrar
from app.config import Settings
from app.db import get_db
from app.deps import get_actor
from app.models import Sesion, TokenCuenta, Usuario
from app.permisos import Actor, requiere_totp
from app.schemas import LoginIn, TotpIn
from app.tiempo import ahora, aware

ACCESO_FALLIDO = "No se pudo iniciar sesión. Revise sus datos; si falló varias veces, espere unos minutos."
_HASH_REFERENCIA = security.hash_password("contraseña-de-referencia")


class ErrorAcceso(Exception):
    def __init__(self, mensaje: str, status_code: int = 401):
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.status_code = status_code


# ---------- Contraseña ----------

def verificar_credenciales(db: Session, settings: Settings, request: Request, identificador: str,
                           password: str) -> Usuario:
    limiter = request.app.state.login_limiter
    if not limiter.allow(f"login:{sesiones.ip_de(request)}"):
        raise ErrorAcceso("Demasiados intentos desde esta conexión. Espere unos minutos.", 429)

    identificador = identificador.strip().lower()
    usuario = db.scalar(select(Usuario).where(or_(Usuario.login == identificador, Usuario.email == identificador)))
    if usuario is None or usuario.password_hash is None:
        security.verify_password(_HASH_REFERENCIA, password)  # tiempo de respuesta similar
        raise ErrorAcceso(ACCESO_FALLIDO)

    momento = ahora()
    if usuario.bloqueado_hasta is not None and aware(usuario.bloqueado_hasta) > momento:
        registrar(db, usuario_id=usuario.id, accion="intento_en_bloqueo", entidad="usuario",
                  entidad_id=usuario.id, ip=sesiones.ip_de(request))
        db.commit()
        raise ErrorAcceso(ACCESO_FALLIDO)

    if not usuario.activo or not security.verify_password(usuario.password_hash, password):
        usuario.intentos_fallidos += 1
        if usuario.intentos_fallidos >= settings.bloqueo_intentos:
            usuario.bloqueado_hasta = momento + timedelta(minutes=settings.bloqueo_minutos)
            usuario.intentos_fallidos = 0
            registrar(db, usuario_id=usuario.id, accion="bloqueo_temporal", entidad="usuario",
                      entidad_id=usuario.id, ip=sesiones.ip_de(request))
        db.commit()
        raise ErrorAcceso(ACCESO_FALLIDO)

    usuario.intentos_fallidos = 0
    usuario.bloqueado_hasta = None
    return usuario


def estado_inicial(usuario: Usuario) -> str:
    if usuario.totp_secret_cifrado:
        return "pendiente_totp"
    if requiere_totp(usuario):
        return "enrolar_totp"
    return "completa"


def _completar(db: Session, settings: Settings, request: Request, sesion: Sesion, valor_dispositivo: str) -> None:
    sesion.estado = "completa"
    sesiones.registrar_dispositivo(db, settings, request, sesion.usuario, valor_dispositivo, sesion)
    registrar(db, usuario_id=sesion.usuario_id, rol=sesion.rol_activo, accion="inicio_sesion",
              entidad="sesion", entidad_id=sesion.id, ip=sesiones.ip_de(request))


def iniciar(db: Session, settings: Settings, request: Request, identificador: str, password: str,
            valor_dispositivo: str) -> tuple[Sesion, str]:
    """Primer paso. Revoca la sesión previa del navegador (rotación) y crea una nueva."""
    usuario = verificar_credenciales(db, settings, request, identificador, password)
    anterior = sesiones.buscar_vigente(db, settings, request)
    if anterior is not None:
        sesiones.revocar(anterior)
    sesion, token = sesiones.crear(db, settings, request, usuario, estado_inicial(usuario))
    if sesion.estado == "completa":
        _completar(db, settings, request, sesion, valor_dispositivo)
    db.commit()
    return sesion, token


def sesion_pendiente(db: Session, settings: Settings, request: Request, estado: str) -> Sesion | None:
    sesion = sesiones.buscar_vigente(db, settings, request)
    return sesion if sesion is not None and sesion.estado == estado else None


def _rotar_a_completa(db: Session, settings: Settings, request: Request, pendiente: Sesion,
                      valor_dispositivo: str) -> tuple[Sesion, str]:
    """Al elevar el nivel de acceso se emite un token nuevo y se invalida el anterior."""
    sesiones.revocar(pendiente)
    sesion, token = sesiones.crear(db, settings, request, pendiente.usuario, "completa")
    sesion.rol_activo = pendiente.rol_activo
    _completar(db, settings, request, sesion, valor_dispositivo)
    db.commit()
    return sesion, token


# ---------- Segundo factor ----------

def verificar_totp(db: Session, settings: Settings, request: Request, pendiente: Sesion, codigo: str,
                   valor_dispositivo: str) -> tuple[Sesion, str]:
    usuario = pendiente.usuario
    if not request.app.state.totp_limiter.allow(f"totp:{usuario.id}"):
        sesiones.revocar(pendiente)
        db.commit()
        raise ErrorAcceso("Demasiados intentos. Inicie sesión de nuevo en unos minutos.", 429)
    secreto = crypto.descifrar(usuario.totp_secret_cifrado, settings.encryption_key)
    if not security.verify_totp(secreto, codigo.strip()):
        raise ErrorAcceso("Código no válido.")
    request.app.state.totp_limiter.reset(f"totp:{usuario.id}")
    return _rotar_a_completa(db, settings, request, pendiente, valor_dispositivo)


def preparar_enrolamiento(db: Session, settings: Settings, usuario: Usuario) -> str:
    """Genera (o reutiliza) un secreto TOTP pendiente de confirmar y devuelve el secreto en claro."""
    if usuario.totp_pendiente_cifrado:
        return crypto.descifrar(usuario.totp_pendiente_cifrado, settings.encryption_key)
    secreto = security.new_totp_secret()
    usuario.totp_pendiente_cifrado = crypto.cifrar(secreto, settings.encryption_key)
    db.commit()
    return secreto


def confirmar_enrolamiento(db: Session, settings: Settings, request: Request, usuario: Usuario,
                           codigo: str) -> None:
    if not request.app.state.totp_limiter.allow(f"totp:{usuario.id}"):
        raise ErrorAcceso("Demasiados intentos. Espere unos minutos.", 429)
    secreto = preparar_enrolamiento(db, settings, usuario)
    if not security.verify_totp(secreto, codigo.strip()):
        raise ErrorAcceso("El código no coincide. Revise que la hora de su teléfono sea correcta.")
    usuario.totp_secret_cifrado = usuario.totp_pendiente_cifrado
    usuario.totp_pendiente_cifrado = None
    registrar(db, usuario_id=usuario.id, accion="activar_totp", entidad="usuario", entidad_id=usuario.id,
              ip=sesiones.ip_de(request))


def enrolar_en_acceso(db: Session, settings: Settings, request: Request, pendiente: Sesion, codigo: str,
                      valor_dispositivo: str) -> tuple[Sesion, str]:
    confirmar_enrolamiento(db, settings, request, pendiente.usuario, codigo)
    return _rotar_a_completa(db, settings, request, pendiente, valor_dispositivo)


# ---------- Enlaces de activación y recuperación ----------

def emitir_token_cuenta(db: Session, settings: Settings, usuario: Usuario, tipo: str) -> str:
    """Crea un enlace de un solo uso. Invalida los anteriores del mismo tipo."""
    momento = ahora()
    db.execute(update(TokenCuenta).where(
        TokenCuenta.usuario_id == usuario.id, TokenCuenta.tipo == tipo, TokenCuenta.usado_en.is_(None),
    ).values(usado_en=momento))
    duracion = (timedelta(hours=settings.activacion_horas) if tipo == "activacion"
                else timedelta(minutes=settings.recuperacion_minutos))
    token = secrets.token_urlsafe(32)
    db.add(TokenCuenta(usuario_id=usuario.id, tipo=tipo, expira_en=momento + duracion,
                       token_hash=security.hash_secret(token, settings.token_hmac_key)))
    return token


def token_cuenta_valido(db: Session, settings: Settings, token: str, tipo: str) -> TokenCuenta | None:
    registro = db.scalar(select(TokenCuenta).where(
        TokenCuenta.token_hash == security.hash_secret(token, settings.token_hmac_key), TokenCuenta.tipo == tipo))
    if registro is None or registro.usado_en is not None or aware(registro.expira_en) <= ahora():
        return None
    return registro


def establecer_password(db: Session, request: Request, registro: TokenCuenta, password: str,
                        confirmacion: str) -> list[str]:
    usuario = db.get(Usuario, registro.usuario_id)
    if password != confirmacion:
        return ["Las contraseñas no coinciden."]
    errores = security.validar_password(password, login=usuario.login, email=usuario.email)
    if errores:
        return errores
    usuario.password_hash = security.hash_password(password)
    usuario.intentos_fallidos = 0
    usuario.bloqueado_hasta = None
    if usuario.activado_en is None:
        usuario.activado_en = ahora()
    registro.usado_en = ahora()
    # Un cambio de contraseña cierra todas las sesiones abiertas
    sesiones.revocar_todas(db, usuario.id)
    registrar(db, usuario_id=usuario.id, accion="activar_cuenta" if registro.tipo == "activacion" else "restablecer_password",
              entidad="usuario", entidad_id=usuario.id, ip=sesiones.ip_de(request))
    db.commit()
    return []


# ---------- API JSON ----------

router = APIRouter(prefix="/api/auth", tags=["autenticación"])


def _paso(sesion: Sesion) -> dict:
    if sesion.estado == "completa":
        return {"paso": "completo", "rol": sesion.rol_activo}
    return {"paso": "totp" if sesion.estado == "pendiente_totp" else "enrolar_totp"}


@router.post("/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    valor = request.cookies.get(sesiones.COOKIE_DISPOSITIVO) or secrets.token_urlsafe(24)
    try:
        sesion, token = iniciar(db, settings, request, body.email, body.password, valor)
    except ErrorAcceso as e:
        raise HTTPException(status_code=e.status_code, detail=e.mensaje) from None
    respuesta = JSONResponse(_paso(sesion))
    sesiones.poner_cookie(respuesta, token, settings)
    respuesta.set_cookie(sesiones.COOKIE_DISPOSITIVO, valor, httponly=True, secure=settings.es_produccion,
                         samesite="lax", max_age=400 * 24 * 3600)
    return respuesta


@router.post("/totp")
def totp(body: TotpIn, request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    pendiente = sesion_pendiente(db, settings, request, "pendiente_totp")
    if pendiente is None:
        raise HTTPException(status_code=401, detail="Inicie sesión primero.")
    valor = request.cookies.get(sesiones.COOKIE_DISPOSITIVO) or secrets.token_urlsafe(24)
    try:
        sesion, token = verificar_totp(db, settings, request, pendiente, body.codigo, valor)
    except ErrorAcceso as e:
        raise HTTPException(status_code=e.status_code, detail=e.mensaje) from None
    respuesta = JSONResponse(_paso(sesion))
    sesiones.poner_cookie(respuesta, token, settings)
    return respuesta


@router.get("/yo")
def quien_soy(actor: Actor = Depends(get_actor)):
    return {"nombre": actor.usuario.nombre, "rol": actor.rol, "roles": list(actor.roles)}


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    sesion = sesiones.buscar_vigente(db, request.app.state.settings, request)
    if sesion is not None:
        sesiones.revocar(sesion)
        db.commit()
    respuesta = JSONResponse({"paso": "cerrada"})
    sesiones.borrar_cookie(respuesta)
    return respuesta
