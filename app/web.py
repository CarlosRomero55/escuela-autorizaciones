"""Capa web común a todas las páginas HTML: sesión, actor, CSRF, menú por rol, avisos y renderizado."""

import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import sesiones
from app.config import Settings
from app.db import get_db
from app.models import AlertaInicioSesion, Sesion
from app.permisos import ETIQUETAS_ROL, Actor, construir_actor, puede
from app.tiempo import ahora, hace, local

RAIZ = Path(__file__).resolve().parents[1]

templates = Jinja2Templates(
    env=Environment(loader=FileSystemLoader(RAIZ / "templates"), autoescape=select_autoescape(["html"]))
)
templates.env.filters["local"] = local
templates.env.filters["hace"] = hace


def nota(valor) -> str:
    """Nota con coma decimal y dos cifras, como se escribe en Ecuador: 8,75."""
    return "" if valor is None else f"{valor:.2f}".replace(".", ",")


def tamano(valor: int) -> str:
    return f"{valor / 1048576:.1f} MB".replace(".", ",") if valor >= 1048576 else f"{max(1, valor // 1024)} KB"


templates.env.filters["nota"] = nota
templates.env.filters["tamano"] = tamano
templates.env.globals["ETIQUETAS_ROL"] = ETIQUETAS_ROL
templates.env.globals["TIPOS"] = {"salida": "Salida pedagógica", "vacuna": "Vacuna", "evento": "Evento", "otro": "Otro"}


def url_con(request: Request, **cambios) -> str:
    """URL actual con algunos parámetros cambiados (paginación y filtros)."""
    url = request.url.include_query_params(**cambios)
    return f"{url.path}?{url.query}"


templates.env.globals["url_con"] = url_con


class NoAutenticado(Exception):
    """Sin sesión: se redirige a la pantalla de acceso."""


class PasoPendiente(Exception):
    """La sesión existe pero falta el segundo factor o su configuración."""

    def __init__(self, destino: str):
        super().__init__(destino)
        self.destino = destino


class SinPermiso(Exception):
    """El rol activo no tiene la función pedida (403)."""


class FormularioVencido(Exception):
    """El token CSRF falta o no coincide (403)."""


@dataclass
class Web:
    request: Request
    db: Session
    settings: Settings
    sesion: Sesion
    actor: Actor

    @property
    def ip(self) -> str | None:
        return sesiones.ip_de(self.request)


def web(request: Request, db: Session = Depends(get_db)) -> Web:
    settings = request.app.state.settings
    sesion = sesiones.buscar_vigente(db, settings, request)
    if sesion is None:
        raise NoAutenticado()
    if sesion.estado == "pendiente_totp":
        raise PasoPendiente("/ingresar/totp")
    if sesion.estado == "enrolar_totp":
        raise PasoPendiente("/ingresar/configurar-totp")
    actor = construir_actor(sesion.usuario, sesion.rol_activo)
    if actor is None:
        raise SinPermiso()
    return Web(request=request, db=db, settings=settings, sesion=sesion, actor=actor)


def requiere(*permisos: str):
    def _dependencia(w: Web = Depends(web)) -> Web:
        if not all(puede(w.actor, p) for p in permisos):
            raise SinPermiso()
        return w
    return _dependencia


def requiere_rol(*roles: str):
    def _dependencia(w: Web = Depends(web)) -> Web:
        if w.actor.rol not in roles:
            raise SinPermiso()
        return w
    return _dependencia


# ---------- CSRF ----------

def verificar_csrf(request: Request, csrf: str = Form(""), db: Session = Depends(get_db)) -> None:
    """Acepta el token de la sesión vigente o, antes de iniciar sesión, el de la cookie de doble envío."""
    candidatos = [request.cookies.get(sesiones.COOKIE_CSRF_PREVIO)]
    sesion = sesiones.buscar_vigente(db, request.app.state.settings, request)
    if sesion is not None:
        candidatos.append(sesion.csrf_token)
    if not csrf or not any(c and hmac.compare_digest(c, csrf) for c in candidatos):
        raise FormularioVencido()


# ---------- Avisos y accesos temporales guardados en la sesión ----------

def avisar(w: Web, mensaje: str, tipo: str = "success") -> None:
    datos = sesiones.leer_datos(w.sesion)
    datos.setdefault("avisos", []).append({"m": mensaje, "t": tipo})
    sesiones.guardar_datos(w.sesion, datos)
    w.db.commit()


def conceder_acceso(w: Web, alumno_id: int, minutos: int = 15) -> None:
    datos = sesiones.leer_datos(w.sesion)
    datos.setdefault("accesos", {})[str(alumno_id)] = (ahora() + timedelta(minutes=minutos)).isoformat()
    sesiones.guardar_datos(w.sesion, datos)
    w.db.commit()


def acceso_vigente(w: Web, alumno_id: int) -> bool:
    vence = sesiones.leer_datos(w.sesion).get("accesos", {}).get(str(alumno_id))
    return bool(vence) and datetime.fromisoformat(vence) > ahora()


# ---------- Menú lateral por rol ----------

def menu(actor: Actor, settings: Settings) -> list[tuple[str, list[tuple[str, str, str, str]]]]:
    grupos: list[tuple[str, list[tuple[str, str, str, str]]]] = [
        ("", [("inicio", "/portal", "bi-house-door", "Inicio")]),
    ]
    if actor.rol == "docente":
        grupos.append(("Docencia", [("rubricas", "/portal/rubricas", "bi-list-check", "Rúbricas")]))
    if actor.rol == "administrador":
        grupos.append(("Gestión académica", [
            ("estructura", "/admin/estructura", "bi-diagram-3", "Estructura académica"),
            ("salones", "/admin/salones", "bi-easel", "Salones"),
            ("alumnos", "/admin/alumnos", "bi-mortarboard", "Alumnos"),
            ("usuarios", "/admin/usuarios", "bi-people", "Usuarios"),
        ]))
    if puede(actor, "autorizaciones.gestionar"):
        grupos.append(("Autorizaciones y recogida", [
            ("campanas", "/admin/campanas", "bi-clipboard-check", "Campañas"),
            ("adultos", "/admin/adultos", "bi-person-check", "Personas autorizadas"),
            ("familias", "/admin/familias", "bi-house-heart", "Familias"),
        ]))
    sistema = []
    if puede(actor, "bitacora.ver"):
        sistema.append(("bitacora", "/admin/bitacora", "bi-journal-text", "Bitácora"))
    if puede(actor, "envios.ver") and settings.demo_mode:
        sistema.append(("envios", "/admin/envios", "bi-envelope", "Mensajes simulados"))
    if sistema:
        grupos.append(("Sistema", sistema))
    grupos.append(("Cuenta", [("perfil", "/portal/perfil", "bi-person-gear", "Mi cuenta")]))
    return grupos


# ---------- Renderizado ----------

def render(w: Web, plantilla: str, contexto: dict | None = None, *, seccion: str = "",
           status_code: int = 200) -> HTMLResponse:
    datos = sesiones.leer_datos(w.sesion)
    avisos = datos.pop("avisos", [])
    if avisos:
        sesiones.guardar_datos(w.sesion, datos)
        w.db.commit()
    alertas = w.db.scalar(select(func.count()).select_from(AlertaInicioSesion).where(
        AlertaInicioSesion.usuario_id == w.actor.id, AlertaInicioSesion.estado == "pendiente"))
    base = {
        "actor": w.actor,
        "usuario": w.actor.usuario,
        "csrf": w.sesion.csrf_token,
        "avisos": avisos,
        "menu": menu(w.actor, w.settings),
        "seccion": seccion,
        "alertas_pendientes": alertas,
        "demo": w.settings.demo_mode,
    }
    return templates.TemplateResponse(w.request, plantilla, {**base, **(contexto or {})}, status_code=status_code)


def render_publico(request: Request, plantilla: str, contexto: dict | None = None, *, status_code: int = 200,
                   csrf: str | None = None) -> HTMLResponse:
    """Páginas sin sesión completa (acceso, activación). Usa una cookie CSRF de doble envío."""
    token = csrf or request.cookies.get(sesiones.COOKIE_CSRF_PREVIO) or secrets.token_urlsafe(32)
    settings = request.app.state.settings
    respuesta = templates.TemplateResponse(request, plantilla, {
        "csrf": token, "demo": settings.demo_mode, **(contexto or {}),
    }, status_code=status_code)
    if csrf is None:
        respuesta.set_cookie(sesiones.COOKIE_CSRF_PREVIO, token, httponly=True, secure=settings.es_produccion,
                             samesite="strict", path="/")
    return respuesta


def no_encontrado(w: Web) -> HTMLResponse:
    """Misma respuesta para lo inexistente y lo que está fuera de alcance."""
    return render(w, "portal/no_encontrado.html", status_code=404)


def pagina_error(request: Request, mensaje: str, status_code: int, titulo: str = "No se puede continuar") -> HTMLResponse:
    return templates.TemplateResponse(request, "error.html", {"mensaje": mensaje, "titulo": titulo},
                                      status_code=status_code)


def volver(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def entero(valor: str | None) -> int | None:
    valor = (valor or "").strip()
    return int(valor) if valor.isdigit() else None
