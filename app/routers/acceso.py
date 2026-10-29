"""Pantallas de acceso: ingreso, segundo factor, configuración del TOTP, activación y recuperación."""

import re
import secrets

import pyotp
import segno
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from markupsafe import Markup
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import crypto, security, sesiones
from app.auth import (
    ErrorAcceso,
    emitir_token_cuenta,
    enrolar_en_acceso,
    establecer_password,
    iniciar,
    preparar_enrolamiento,
    sesion_pendiente,
    token_cuenta_valido,
    verificar_totp,
)
from app.db import get_db
from app.models import EnvioSaliente, Usuario
from app.permisos import requiere_totp
from app.web import render_publico, verificar_csrf, volver

router = APIRouter(include_in_schema=False)

USUARIOS_DEMO = [
    ("admin", "Administración"),
    ("sofia", "Coordinación"),
    ("rector", "Directivo (toda la escuela)"),
    ("dir.matematica", "Directivo del área de Matemática"),
    ("docente", "Docente tutora de 5.º A"),
    ("representante", "Representante"),
    ("estudiante", "Estudiante de 5.º A"),
    ("consejeria", "Consejería"),
    ("auditor", "Auditoría"),
    ("porteria", "Portería"),
]


def _con_sesion(respuesta: RedirectResponse, request: Request, token: str, valor_dispositivo: str) -> RedirectResponse:
    settings = request.app.state.settings
    sesiones.poner_cookie(respuesta, token, settings)
    respuesta.set_cookie(sesiones.COOKIE_DISPOSITIVO, valor_dispositivo, httponly=True,
                         secure=settings.es_produccion, samesite="lax", max_age=400 * 24 * 3600, path="/")
    return respuesta


def _valor_dispositivo(request: Request) -> str:
    valor = request.cookies.get(sesiones.COOKIE_DISPOSITIVO)
    return valor if valor and len(valor) <= 64 else secrets.token_urlsafe(24)


def _destino(estado: str) -> str:
    return {"pendiente_totp": "/ingresar/totp", "enrolar_totp": "/ingresar/configurar-totp"}.get(estado, "/portal")


# ---------- Ingreso ----------

@router.get("/ingresar")
def ver_ingreso(request: Request, db: Session = Depends(get_db)):
    sesion = sesiones.buscar_vigente(db, request.app.state.settings, request)
    if sesion is not None:
        return volver(_destino(sesion.estado))
    return _pantalla_ingreso(request)


def _pantalla_ingreso(request: Request, **extra):
    settings = request.app.state.settings
    status = extra.pop("status_code", 200)
    return render_publico(request, "acceso/ingresar.html", {
        "usuarios_demo": USUARIOS_DEMO if settings.demo_mode else None,
        "demo_password": settings.demo_password, **extra,
    }, status_code=status)


@router.post("/ingresar", dependencies=[Depends(verificar_csrf)])
def enviar_ingreso(request: Request, identificador: str = Form(""), password: str = Form(""),
                   db: Session = Depends(get_db)):
    settings = request.app.state.settings
    valor = _valor_dispositivo(request)
    try:
        sesion, token = iniciar(db, settings, request, identificador, password, valor)
    except ErrorAcceso as e:
        return _pantalla_ingreso(request, error=e.mensaje, identificador=identificador, status_code=e.status_code)
    return _con_sesion(volver(_destino(sesion.estado)), request, token, valor)


@router.post("/salir", dependencies=[Depends(verificar_csrf)])
def salir(request: Request, db: Session = Depends(get_db)):
    sesion = sesiones.buscar_vigente(db, request.app.state.settings, request)
    if sesion is not None:
        sesiones.revocar(sesion)
        db.commit()
    respuesta = volver("/ingresar")
    sesiones.borrar_cookie(respuesta)
    return respuesta


# ---------- Segundo factor ----------

def _codigo_demo(request: Request, usuario: Usuario, cifrado: str | None) -> str | None:
    """Solo en la demo pública: muestra el código vigente para que cualquiera pueda recorrerla."""
    settings = request.app.state.settings
    if not settings.demo_mode or not cifrado:
        return None
    return pyotp.TOTP(crypto.descifrar(cifrado, settings.encryption_key)).now()


@router.get("/ingresar/totp")
def ver_totp(request: Request, db: Session = Depends(get_db)):
    pendiente = sesion_pendiente(db, request.app.state.settings, request, "pendiente_totp")
    if pendiente is None:
        return volver("/ingresar")
    return render_publico(request, "acceso/totp.html", {
        "codigo_demo": _codigo_demo(request, pendiente.usuario, pendiente.usuario.totp_secret_cifrado),
    }, csrf=pendiente.csrf_token)


@router.post("/ingresar/totp", dependencies=[Depends(verificar_csrf)])
def enviar_totp(request: Request, codigo: str = Form(""), db: Session = Depends(get_db)):
    settings = request.app.state.settings
    pendiente = sesion_pendiente(db, settings, request, "pendiente_totp")
    if pendiente is None:
        return volver("/ingresar")
    if not re.fullmatch(r"\d{6}", codigo.strip()):
        return render_publico(request, "acceso/totp.html", {"error": "Escriba los 6 dígitos de su aplicación."},
                              status_code=422, csrf=pendiente.csrf_token)
    valor = _valor_dispositivo(request)
    try:
        sesion, token = verificar_totp(db, settings, request, pendiente, codigo, valor)
    except ErrorAcceso as e:
        if e.status_code == 429:
            return _pantalla_ingreso(request, error=e.mensaje, status_code=429)
        return render_publico(request, "acceso/totp.html", {
            "error": e.mensaje,
            "codigo_demo": _codigo_demo(request, pendiente.usuario, pendiente.usuario.totp_secret_cifrado),
        }, status_code=e.status_code, csrf=pendiente.csrf_token)
    return _con_sesion(volver("/portal"), request, token, valor)


def qr_svg(usuario: Usuario, secreto: str) -> Markup:
    uri = security.totp_provisioning_uri(secreto, usuario.login, issuer="Escuela demo")
    return Markup(segno.make(uri, error="m").svg_inline(scale=5, border=2, omitsize=True, title="Código QR para la aplicación"))


@router.get("/ingresar/configurar-totp")
def ver_enrolar(request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    pendiente = sesion_pendiente(db, settings, request, "enrolar_totp")
    if pendiente is None:
        return volver("/ingresar")
    secreto = preparar_enrolamiento(db, settings, pendiente.usuario)
    return render_publico(request, "acceso/configurar_totp.html", {
        "qr": qr_svg(pendiente.usuario, secreto), "secreto": secreto,
        "codigo_demo": pyotp.TOTP(secreto).now() if settings.demo_mode else None,
    }, csrf=pendiente.csrf_token)


@router.post("/ingresar/configurar-totp", dependencies=[Depends(verificar_csrf)])
def enviar_enrolar(request: Request, codigo: str = Form(""), db: Session = Depends(get_db)):
    settings = request.app.state.settings
    pendiente = sesion_pendiente(db, settings, request, "enrolar_totp")
    if pendiente is None:
        return volver("/ingresar")
    valor = _valor_dispositivo(request)
    try:
        sesion, token = enrolar_en_acceso(db, settings, request, pendiente, codigo, valor)
    except ErrorAcceso as e:
        secreto = preparar_enrolamiento(db, settings, pendiente.usuario)
        return render_publico(request, "acceso/configurar_totp.html", {
            "qr": qr_svg(pendiente.usuario, secreto), "secreto": secreto, "error": e.mensaje,
            "codigo_demo": pyotp.TOTP(secreto).now() if settings.demo_mode else None,
        }, status_code=e.status_code, csrf=pendiente.csrf_token)
    return _con_sesion(volver("/portal"), request, token, valor)


# ---------- Activación de cuenta y recuperación ----------

ENLACE_INVALIDO = "Este enlace no es válido, ya se usó o venció. Pida uno nuevo a la escuela."


@router.get("/cuenta/activar/{token}")
def ver_activar(token: str, request: Request, db: Session = Depends(get_db)):
    registro = token_cuenta_valido(db, request.app.state.settings, token, "activacion")
    if registro is None:
        return render_publico(request, "acceso/mensaje.html", {"titulo": "Enlace no válido", "mensaje": ENLACE_INVALIDO},
                              status_code=404)
    usuario = db.get(Usuario, registro.usuario_id)
    return render_publico(request, "acceso/nueva_password.html", {
        "titulo": "Active su cuenta", "usuario": usuario, "accion": f"/cuenta/activar/{token}",
        "totp_obligatorio": requiere_totp(usuario),
    })


@router.post("/cuenta/activar/{token}", dependencies=[Depends(verificar_csrf)])
def enviar_activar(token: str, request: Request, password: str = Form(""), confirmacion: str = Form(""),
                   db: Session = Depends(get_db)):
    return _guardar_password(token, "activacion", request, password, confirmacion, db)


@router.get("/cuenta/restablecer/{token}")
def ver_restablecer(token: str, request: Request, db: Session = Depends(get_db)):
    registro = token_cuenta_valido(db, request.app.state.settings, token, "recuperacion")
    if registro is None:
        return render_publico(request, "acceso/mensaje.html", {"titulo": "Enlace no válido", "mensaje": ENLACE_INVALIDO},
                              status_code=404)
    return render_publico(request, "acceso/nueva_password.html", {
        "titulo": "Nueva contraseña", "usuario": db.get(Usuario, registro.usuario_id),
        "accion": f"/cuenta/restablecer/{token}",
    })


@router.post("/cuenta/restablecer/{token}", dependencies=[Depends(verificar_csrf)])
def enviar_restablecer(token: str, request: Request, password: str = Form(""), confirmacion: str = Form(""),
                       db: Session = Depends(get_db)):
    return _guardar_password(token, "recuperacion", request, password, confirmacion, db)


def _guardar_password(token: str, tipo: str, request: Request, password: str, confirmacion: str, db: Session):
    registro = token_cuenta_valido(db, request.app.state.settings, token, tipo)
    if registro is None:
        return render_publico(request, "acceso/mensaje.html", {"titulo": "Enlace no válido", "mensaje": ENLACE_INVALIDO},
                              status_code=404)
    errores = establecer_password(db, request, registro, password, confirmacion)
    if errores:
        usuario = db.get(Usuario, registro.usuario_id)
        return render_publico(request, "acceso/nueva_password.html", {
            "titulo": "Active su cuenta" if tipo == "activacion" else "Nueva contraseña", "usuario": usuario,
            "accion": request.url.path, "errores": errores, "totp_obligatorio": requiere_totp(usuario),
        }, status_code=422)
    return render_publico(request, "acceso/mensaje.html", {
        "titulo": "Contraseña guardada",
        "mensaje": "Ya puede ingresar al portal con su nueva contraseña.",
        "enlace": ("/ingresar", "Ingresar"),
    })


@router.get("/cuenta/recuperar")
def ver_recuperar(request: Request):
    return render_publico(request, "acceso/recuperar.html")


@router.post("/cuenta/recuperar", dependencies=[Depends(verificar_csrf)])
def enviar_recuperar(request: Request, email: str = Form(""), db: Session = Depends(get_db)):
    settings = request.app.state.settings
    if not request.app.state.login_limiter.allow(f"recuperar:{sesiones.ip_de(request)}"):
        return render_publico(request, "acceso/recuperar.html",
                              {"error": "Demasiadas solicitudes. Espere unos minutos."}, status_code=429)
    email = email.strip().lower()
    usuario = db.scalar(select(Usuario).where(or_(Usuario.email == email, Usuario.login == email))) if email else None
    if usuario is not None and usuario.activo and usuario.email and usuario.password_hash:
        token = emitir_token_cuenta(db, settings, usuario, "recuperacion")
        enlace = f"{str(request.base_url).rstrip('/')}/cuenta/restablecer/{token}"
        db.add(EnvioSaliente(
            canal="correo", destino=usuario.email, asunto="Restablecer su contraseña",
            cuerpo=(f"Recibimos una solicitud para restablecer su contraseña. Use este enlace en los próximos "
                    f"{settings.recuperacion_minutos} minutos: {enlace}\n\nSi no la pidió, ignore este mensaje."),
        ))
        db.commit()
    # La respuesta es igual exista o no la cuenta, para no revelar qué correos están registrados
    return render_publico(request, "acceso/mensaje.html", {
        "titulo": "Revise su correo",
        "mensaje": "Si el dato corresponde a una cuenta activa, enviamos un enlace para restablecer la contraseña. "
                   "Vence en pocos minutos y sirve una sola vez.",
        "enlace": ("/ingresar", "Volver al ingreso"),
    })


@router.get("/admin/login")
def login_antiguo():
    return RedirectResponse("/ingresar", status_code=301)
