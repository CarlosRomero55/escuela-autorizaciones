"""Sesiones guardadas en el servidor y registro de dispositivos.

El navegador solo guarda un token aleatorio en una cookie HttpOnly; la base de datos guarda su HMAC.
Eso permite listar las sesiones activas, cerrarlas a distancia y expirarlas por inactividad.
"""

import json
import re
import secrets
from datetime import timedelta

from fastapi import Request, Response
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import security
from app.config import Settings
from app.models import AlertaInicioSesion, DispositivoUsuario, EnvioSaliente, Sesion, Usuario
from app.permisos import PRIORIDAD_ROLES, roles_vigentes
from app.tiempo import ahora, aware, local

COOKIE_SESION = "sid"
COOKIE_DISPOSITIVO = "did"
COOKIE_CSRF_PREVIO = "csrf_pre"
# Para no escribir en la base en cada petición, la actividad se actualiza como mucho cada minuto
_INTERVALO_ACTIVIDAD = timedelta(seconds=60)


def _hash(valor: str, settings: Settings) -> str:
    return security.hash_secret(valor, settings.token_hmac_key)


def ip_de(request: Request) -> str | None:
    return request.client.host if request.client else None


def describir_navegador(user_agent: str | None) -> str:
    ua = user_agent or ""
    navegador = next((n for patron, n in [
        (r"Edg/", "Edge"), (r"OPR/", "Opera"), (r"Firefox/", "Firefox"),
        (r"Chrome/", "Chrome"), (r"Safari/", "Safari"),
    ] if re.search(patron, ua)), "Navegador")
    sistema = next((s for patron, s in [
        (r"CrOS", "Chromebook"), (r"Android", "Android"), (r"iPhone|iPad", "iOS"),
        (r"Windows", "Windows"), (r"Mac OS X", "macOS"), (r"Linux", "Linux"),
    ] if re.search(patron, ua)), "dispositivo desconocido")
    return f"{navegador} en {sistema}"


# ---------- Datos de la sesión (avisos y accesos temporales) ----------

def leer_datos(sesion: Sesion) -> dict:
    try:
        return json.loads(sesion.datos) if sesion.datos else {}
    except ValueError:
        return {}


def guardar_datos(sesion: Sesion, datos: dict) -> None:
    sesion.datos = json.dumps(datos, ensure_ascii=False)


# ---------- Ciclo de vida ----------

def rol_inicial(usuario: Usuario) -> str | None:
    nombres = {r.rol for r in roles_vigentes(usuario)}
    return next((r for r in PRIORIDAD_ROLES if r in nombres), None)


def crear(db: Session, settings: Settings, request: Request, usuario: Usuario, estado: str,
          dispositivo_id: int | None = None) -> tuple[Sesion, str]:
    token = secrets.token_urlsafe(32)
    momento = ahora()
    sesion = Sesion(
        token_hash=_hash(token, settings),
        usuario_id=usuario.id,
        dispositivo_id=dispositivo_id,
        estado=estado,
        rol_activo=rol_inicial(usuario),
        csrf_token=secrets.token_urlsafe(32),
        ip=ip_de(request),
        user_agent=(request.headers.get("user-agent") or "")[:300],
        ultima_actividad=momento,
        expira_en=momento + timedelta(hours=settings.sesion_maxima_horas),
    )
    db.add(sesion)
    db.flush()
    return sesion, token


def buscar_vigente(db: Session, settings: Settings, request: Request) -> Sesion | None:
    token = request.cookies.get(COOKIE_SESION)
    if not token:
        return None
    sesion = db.scalar(select(Sesion).where(Sesion.token_hash == _hash(token, settings)))
    if sesion is None or sesion.revocada_en is not None or not sesion.usuario.activo:
        return None
    momento = ahora()
    if aware(sesion.expira_en) <= momento:
        return None
    if aware(sesion.ultima_actividad) <= momento - timedelta(minutes=settings.sesion_inactividad_minutos):
        return None
    if momento - aware(sesion.ultima_actividad) > _INTERVALO_ACTIVIDAD:
        sesion.ultima_actividad = momento
        db.commit()
    return sesion


def revocar(sesion: Sesion) -> None:
    if sesion.revocada_en is None:
        sesion.revocada_en = ahora()


def revocar_todas(db: Session, usuario_id: int, excepto_id: int | None = None) -> int:
    consulta = update(Sesion).where(Sesion.usuario_id == usuario_id, Sesion.revocada_en.is_(None))
    if excepto_id is not None:
        consulta = consulta.where(Sesion.id != excepto_id)
    return db.execute(consulta.values(revocada_en=ahora())).rowcount


def activas(db: Session, settings: Settings, usuario_id: int) -> list[Sesion]:
    momento = ahora()
    limite = momento - timedelta(minutes=settings.sesion_inactividad_minutos)
    sesiones = db.scalars(select(Sesion).where(
        Sesion.usuario_id == usuario_id, Sesion.revocada_en.is_(None), Sesion.estado == "completa",
    ).order_by(Sesion.ultima_actividad.desc())).all()
    return [s for s in sesiones if aware(s.expira_en) > momento and aware(s.ultima_actividad) > limite]


# ---------- Cookies ----------

def poner_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(COOKIE_SESION, token, httponly=True, secure=settings.es_produccion,
                        samesite="strict", max_age=settings.sesion_maxima_horas * 3600, path="/")


def borrar_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_SESION, path="/")


def cookie_dispositivo(request: Request, response: Response, settings: Settings) -> str:
    """Identificador aleatorio y persistente del navegador. Solo se guarda su HMAC."""
    valor = request.cookies.get(COOKIE_DISPOSITIVO)
    if not valor or len(valor) > 64:
        valor = secrets.token_urlsafe(24)
    response.set_cookie(COOKIE_DISPOSITIVO, valor, httponly=True, secure=settings.es_produccion,
                        samesite="lax", max_age=400 * 24 * 3600, path="/")
    return valor


# ---------- Dispositivos y alertas de inicio inusual ----------

def registrar_dispositivo(db: Session, settings: Settings, request: Request, usuario: Usuario,
                          valor_cookie: str, sesion: Sesion) -> DispositivoUsuario:
    """Asocia la sesión a un dispositivo. Si es nuevo y el usuario ya tenía otros, crea una alerta."""
    huella = _hash(valor_cookie, settings)
    dispositivo = db.scalar(select(DispositivoUsuario).where(
        DispositivoUsuario.usuario_id == usuario.id, DispositivoUsuario.huella_hash == huella))
    descripcion = describir_navegador(request.headers.get("user-agent"))
    if dispositivo is None:
        anteriores = db.scalar(select(func.count()).select_from(DispositivoUsuario)
                               .where(DispositivoUsuario.usuario_id == usuario.id))
        dispositivo = DispositivoUsuario(usuario_id=usuario.id, huella_hash=huella, descripcion=descripcion)
        db.add(dispositivo)
        db.flush()
        if anteriores:
            _alertar(db, usuario, sesion, f"Nuevo dispositivo: {descripcion}")
    else:
        dispositivo.ultima_vez = ahora()
        dispositivo.descripcion = descripcion
        if not dispositivo.confiable:
            _alertar(db, usuario, sesion, f"Acceso desde un dispositivo marcado como no reconocido: {descripcion}")
    sesion.dispositivo_id = dispositivo.id
    return dispositivo


def _alertar(db: Session, usuario: Usuario, sesion: Sesion, motivo: str) -> None:
    db.add(AlertaInicioSesion(usuario_id=usuario.id, sesion_id=sesion.id, motivo=motivo))
    if usuario.email:
        db.add(EnvioSaliente(
            canal="correo", destino=usuario.email, asunto="Nuevo inicio de sesión en su cuenta",
            cuerpo=(f"Se inició sesión en su cuenta el {local(ahora())} (hora de Ecuador). {motivo}.\n\n"
                    "Si fue usted, no necesita hacer nada. Si no lo reconoce, ingrese al portal, abra "
                    "«Mi cuenta» y pulse «No fui yo»: se cerrará esa sesión. Luego cambie su contraseña."),
        ))
