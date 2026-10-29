"""Panel de administración en HTML para la coordinación.

Formularios clásicos con recarga de página: funciona sin JavaScript y respeta la CSP estricta.
Cada formulario lleva un token CSRF ligado a la sesión, además de la cookie SameSite=strict.
"""

import csv
import hmac
import io
import re
import secrets
from datetime import timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import services
from app.auth import completar_totp, iniciar_sesion
from app.db import get_db
from app.models import (
    AdultoAutorizado,
    Alumno,
    BitacoraAuditoria,
    Campana,
    CampanaSalon,
    Familia,
    FamiliaAlumno,
    Incidente,
    RespuestaAutorizacion,
    Salon,
    TokenAcceso,
    UsuarioStaff,
)
from app.routers.familias import templates
from app.tiempo import ahora, aware, desde_formulario, local

router = APIRouter(prefix="/admin", include_in_schema=False)

GESTION = ("administrador", "coordinadora")
TIPOS = {"salida": "Salida pedagógica", "vacuna": "Vacuna", "evento": "Evento", "otro": "Otro"}
ESTADOS_ADULTO = {"pendiente": "Pendientes", "validado": "Validados", "rechazado": "Rechazados"}
USUARIOS_DEMO = [
    ("sofia@demo.test", "Coordinación"),
    ("admin@demo.test", "Administración"),
    ("porteria@demo.test", "Portería"),
    ("maestra@demo.test", "Maestra"),
]

templates.env.filters["local"] = local
templates.env.globals["TIPOS"] = TIPOS


class NoAutenticado(Exception):
    """No hay sesión: se redirige al inicio de sesión."""


class SinPermiso(Exception):
    """Hay sesión, pero el rol no tiene acceso al panel."""


class FormularioVencido(Exception):
    """El token CSRF falta o no coincide con el de la sesión."""


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def verificar_csrf(request: Request, csrf: str = Form("")) -> None:
    esperado = request.session.get("csrf")
    if not esperado or not csrf or not hmac.compare_digest(esperado, csrf):
        raise FormularioVencido()


def usuario_panel(request: Request, db: Session = Depends(get_db)) -> UsuarioStaff:
    uid = request.session.get("uid")
    if uid is None:
        raise NoAutenticado()
    usuario = db.get(UsuarioStaff, uid)
    if usuario is None or not usuario.activo:
        request.session.clear()
        raise NoAutenticado()
    if usuario.rol not in GESTION:
        raise SinPermiso()
    return usuario


def avisar(request: Request, mensaje: str, tipo: str = "ok") -> None:
    request.session["avisos"] = [*request.session.get("avisos", []), {"m": mensaje, "t": tipo}]


def render(request: Request, plantilla: str, contexto: dict | None = None, *,
           usuario: UsuarioStaff | None = None, seccion: str = "", status_code: int = 200) -> HTMLResponse:
    datos = {
        "csrf": csrf_token(request),
        "avisos": request.session.pop("avisos", []),
        "usuario": usuario,
        "seccion": seccion,
        **(contexto or {}),
    }
    return templates.TemplateResponse(request, plantilla, datos, status_code=status_code)


def pagina_error(request: Request, mensaje: str, status_code: int) -> HTMLResponse:
    return render(request, "admin/error.html", {"mensaje": mensaje}, status_code=status_code)


def volver(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _entero(valor: str) -> int | None:
    valor = (valor or "").strip()
    return int(valor) if valor.isdigit() else None


# ---------- Acceso ----------

@router.get("/login", response_class=HTMLResponse)
def ver_login(request: Request):
    if request.session.get("uid"):
        return volver("/admin")
    return _pantalla_login(request)


def _pantalla_login(request: Request, **extra) -> HTMLResponse:
    settings = request.app.state.settings
    contexto = {"demo": USUARIOS_DEMO if settings.demo_mode else None, "demo_password": settings.demo_password}
    contexto.update({k: v for k, v in extra.items() if k != "status_code"})
    return render(request, "admin/login.html", contexto, status_code=extra.get("status_code", 200))


@router.post("/login", dependencies=[Depends(verificar_csrf)])
def enviar_login(request: Request, email: str = Form(""), password: str = Form(""), db: Session = Depends(get_db)):
    try:
        resultado = iniciar_sesion(request, db, email, password)
    except HTTPException as e:
        return _pantalla_login(request, error=e.detail, email=email, status_code=e.status_code)
    return volver("/admin/totp" if resultado["paso"] == "totp" else "/admin")


@router.get("/totp", response_class=HTMLResponse)
def ver_totp(request: Request):
    if request.session.get("pendiente_uid") is None:
        return volver("/admin/login")
    return render(request, "admin/totp.html")


@router.post("/totp", dependencies=[Depends(verificar_csrf)])
def enviar_totp(request: Request, codigo: str = Form(""), db: Session = Depends(get_db)):
    codigo = codigo.strip()
    if not re.fullmatch(r"\d{6}", codigo):
        return render(request, "admin/totp.html",
                      {"error": "Escriba los 6 dígitos que muestra su aplicación de autenticación."}, status_code=422)
    try:
        completar_totp(request, db, codigo)
    except HTTPException as e:
        if request.session.get("pendiente_uid") is None:
            return _pantalla_login(request, error=e.detail, status_code=e.status_code)
        return render(request, "admin/totp.html", {"error": e.detail}, status_code=e.status_code)
    return volver("/admin")


@router.post("/logout", dependencies=[Depends(verificar_csrf)])
def cerrar_sesion(request: Request):
    request.session.clear()
    return volver("/admin/login")


# ---------- Inicio ----------

@router.get("", response_class=HTMLResponse)
def inicio(request: Request, db: Session = Depends(get_db), usuario: UsuarioStaff = Depends(usuario_panel)):
    momento = ahora()
    vigentes = []
    for campana in db.scalars(select(Campana).order_by(Campana.fecha_limite)).all():
        if aware(campana.fecha_limite) > momento:
            vigentes.append({"campana": campana,
                             "totales": services.totalizar(services.progreso_campana(db, campana.id))})
    resumen = {
        "alumnos": db.scalar(select(func.count()).select_from(Alumno).where(Alumno.activo.is_(True))),
        "adultos_pendientes": db.scalar(select(func.count()).select_from(AdultoAutorizado)
                                        .where(AdultoAutorizado.estado == "pendiente")),
        "incidentes": db.scalar(select(func.count()).select_from(Incidente)
                                .where(Incidente.creado_en >= momento - timedelta(days=7))),
        "campanas": len(vigentes),
    }
    return render(request, "admin/inicio.html", {"resumen": resumen, "vigentes": vigentes},
                  usuario=usuario, seccion="inicio")


# ---------- Salones ----------

@router.get("/salones", response_class=HTMLResponse)
def salones(request: Request, db: Session = Depends(get_db), usuario: UsuarioStaff = Depends(usuario_panel)):
    filas = db.execute(
        select(Salon, func.count(Alumno.id))
        .outerjoin(Alumno, (Alumno.salon_id == Salon.id) & Alumno.activo.is_(True))
        .group_by(Salon.id).order_by(Salon.nombre)
    ).all()
    return render(request, "admin/salones.html", {"salones": filas}, usuario=usuario, seccion="salones")


@router.post("/salones", dependencies=[Depends(verificar_csrf)])
def crear_salon(request: Request, nombre: str = Form(""), grado: str = Form(""),
                db: Session = Depends(get_db), usuario: UsuarioStaff = Depends(usuario_panel)):
    if not nombre.strip() or not grado.strip() or len(nombre) > 60 or len(grado) > 20:
        avisar(request, "Escriba el nombre (hasta 60 caracteres) y el grado (hasta 20).", "error")
        return volver("/admin/salones")
    try:
        salon = services.crear_salon(db, usuario_id=usuario.id, ip=_ip(request), nombre=nombre, grado=grado)
        avisar(request, f"Salón {salon.nombre} creado.")
    except services.ErrorNegocio as e:
        avisar(request, e.mensaje, "error")
    return volver("/admin/salones")


# ---------- Alumnos ----------

@router.get("/alumnos", response_class=HTMLResponse)
def alumnos(request: Request, salon_id: str = "", q: str = "", db: Session = Depends(get_db),
            usuario: UsuarioStaff = Depends(usuario_panel)):
    filtro_salon = _entero(salon_id)
    consulta = select(Alumno, Salon.nombre).join(Salon, Salon.id == Alumno.salon_id)
    if filtro_salon is not None:
        consulta = consulta.where(Alumno.salon_id == filtro_salon)
    if q.strip():
        consulta = consulta.where(Alumno.nombre.ilike(f"%{q.strip()[:60]}%"))
    filas = db.execute(consulta.order_by(Salon.nombre, Alumno.nombre).limit(500)).all()
    return render(request, "admin/alumnos.html", {
        "filas": filas,
        "salones": db.scalars(select(Salon).order_by(Salon.nombre)).all(),
        "salon_id": filtro_salon,
        "q": q,
    }, usuario=usuario, seccion="alumnos")


@router.post("/alumnos", dependencies=[Depends(verificar_csrf)])
def crear_alumno(request: Request, codigo_interno: str = Form(""), nombre: str = Form(""), salon_id: str = Form(""),
                 db: Session = Depends(get_db), usuario: UsuarioStaff = Depends(usuario_panel)):
    salon = _entero(salon_id)
    if not codigo_interno.strip() or not nombre.strip() or salon is None:
        avisar(request, "Complete código, nombre y salón.", "error")
    elif len(codigo_interno) > 20 or len(nombre) > 120:
        avisar(request, "El código admite hasta 20 caracteres y el nombre hasta 120.", "error")
    else:
        try:
            alumno = services.crear_alumno(db, usuario_id=usuario.id, ip=_ip(request),
                                           codigo_interno=codigo_interno, nombre=nombre, salon_id=salon)
            avisar(request, f"Alumno {alumno.nombre} agregado.")
        except services.ErrorNegocio as e:
            avisar(request, e.mensaje, "error")
    return volver("/admin/alumnos")


@router.post("/alumnos/{alumno_id}/activo", dependencies=[Depends(verificar_csrf)])
def cambiar_activo(request: Request, alumno_id: int, activo: str = Form(""),
                   db: Session = Depends(get_db), usuario: UsuarioStaff = Depends(usuario_panel)):
    try:
        alumno = services.cambiar_activo_alumno(db, usuario_id=usuario.id, ip=_ip(request),
                                                alumno_id=alumno_id, activo=activo == "1")
        avisar(request, f"{alumno.nombre}: {'activo' if alumno.activo else 'inactivo'}.")
    except services.ErrorNegocio as e:
        avisar(request, e.mensaje, "error")
    return volver("/admin/alumnos")


@router.get("/alumnos/importar", response_class=HTMLResponse)
def ver_importar(request: Request, usuario: UsuarioStaff = Depends(usuario_panel)):
    return render(request, "admin/importar.html", {"columnas": services.COLUMNAS_CSV},
                  usuario=usuario, seccion="alumnos")


@router.post("/alumnos/importar/vista-previa", dependencies=[Depends(verificar_csrf)])
def vista_previa(request: Request, archivo: UploadFile = File(...), db: Session = Depends(get_db),
                 usuario: UsuarioStaff = Depends(usuario_panel)):
    try:
        texto = services.decodificar_csv(archivo.file.read(services.MAX_BYTES_CSV + 1))
        filas = services.analizar_csv(db, texto)
    except services.ErrorNegocio as e:
        avisar(request, e.mensaje, "error")
        return volver("/admin/alumnos/importar")
    conteo = {estado: sum(1 for f in filas if f.estado == estado) for estado in ("valida", "duplicada", "error")}
    return render(request, "admin/importar_vista.html", {"filas": filas, "conteo": conteo, "texto": texto},
                  usuario=usuario, seccion="alumnos")


@router.post("/alumnos/importar", dependencies=[Depends(verificar_csrf)])
def confirmar_importacion(request: Request, texto: str = Form(""), db: Session = Depends(get_db),
                          usuario: UsuarioStaff = Depends(usuario_panel)):
    # El archivo se vuelve a analizar: no se confía en lo que se mostró en la vista previa
    try:
        creados, omitidos = services.importar_alumnos(db, usuario_id=usuario.id, ip=_ip(request), texto=texto)
        avisar(request, f"Se importaron {creados} alumnos. Filas omitidas: {omitidos}.")
    except services.ErrorNegocio as e:
        avisar(request, e.mensaje, "error")
        return volver("/admin/alumnos/importar")
    return volver("/admin/alumnos")


# ---------- Familias ----------

def _enlace_whatsapp(telefono: str | None, mensaje: str) -> str | None:
    """Enlace wa.me para enviar el mensaje desde el WhatsApp de la escuela. Asume números de Ecuador."""
    digitos = re.sub(r"\D", "", telefono or "")
    if not digitos:
        return None
    if digitos.startswith("0"):
        digitos = "593" + digitos[1:]
    return f"https://wa.me/{digitos}?text={quote(mensaje)}"


@router.get("/familias", response_class=HTMLResponse)
def familias(request: Request, db: Session = Depends(get_db), usuario: UsuarioStaff = Depends(usuario_panel)):
    momento = ahora()
    vinculos: dict[int, list[str]] = {}
    for familia_id, nombre in db.execute(
        select(FamiliaAlumno.familia_id, Alumno.nombre).join(Alumno, Alumno.id == FamiliaAlumno.alumno_id)
        .order_by(Alumno.nombre)
    ).all():
        vinculos.setdefault(familia_id, []).append(nombre)
    vencimientos = {
        familia_id: expira for familia_id, expira in db.execute(
            select(TokenAcceso.familia_id, func.max(TokenAcceso.expira_en)).group_by(TokenAcceso.familia_id)
        ).all()
        if aware(expira) > momento
    }
    return render(request, "admin/familias.html", {
        "familias": db.scalars(select(Familia).order_by(Familia.nombre_familia)).all(),
        "vinculos": vinculos,
        "vencimientos": vencimientos,
        "alumnos": db.execute(select(Alumno, Salon.nombre).join(Salon, Salon.id == Alumno.salon_id)
                              .where(Alumno.activo.is_(True)).order_by(Salon.nombre, Alumno.nombre)).all(),
    }, usuario=usuario, seccion="familias")


@router.post("/familias", dependencies=[Depends(verificar_csrf)])
def crear_familia(request: Request, nombre_familia: str = Form(""), telefono: str = Form(""), email: str = Form(""),
                  consentimiento: str = Form(""), db: Session = Depends(get_db),
                  usuario: UsuarioStaff = Depends(usuario_panel)):
    if not nombre_familia.strip() or len(nombre_familia) > 120 or len(telefono) > 20 or len(email) > 160:
        avisar(request, "Revise el nombre de la familia, el teléfono y el correo.", "error")
        return volver("/admin/familias")
    try:
        familia = services.crear_familia(db, usuario_id=usuario.id, ip=_ip(request), nombre_familia=nombre_familia,
                                         telefono=telefono, email=email,
                                         consentimiento_mensajes=consentimiento == "on")
        avisar(request, f"{familia.nombre_familia} agregada.")
    except services.ErrorNegocio as e:
        avisar(request, e.mensaje, "error")
    return volver("/admin/familias")


@router.post("/familias/{familia_id}/alumnos", dependencies=[Depends(verificar_csrf)])
def vincular(request: Request, familia_id: int, alumno_id: str = Form(""), db: Session = Depends(get_db),
             usuario: UsuarioStaff = Depends(usuario_panel)):
    alumno = _entero(alumno_id)
    if alumno is None:
        avisar(request, "Elija un alumno.", "error")
        return volver("/admin/familias")
    try:
        services.vincular_alumno(db, usuario_id=usuario.id, ip=_ip(request), familia_id=familia_id, alumno_id=alumno)
        avisar(request, "Alumno vinculado a la familia.")
    except services.ErrorNegocio as e:
        avisar(request, e.mensaje, "error")
    return volver("/admin/familias")


@router.post("/familias/{familia_id}/enlace", dependencies=[Depends(verificar_csrf)], response_class=HTMLResponse)
def generar_enlace(request: Request, familia_id: int, db: Session = Depends(get_db),
                   usuario: UsuarioStaff = Depends(usuario_panel)):
    try:
        token, expira = services.generar_enlace(db, settings=request.app.state.settings, usuario_id=usuario.id,
                                                ip=_ip(request), familia_id=familia_id)
    except services.ErrorNegocio as e:
        avisar(request, e.mensaje, "error")
        return volver("/admin/familias")
    familia = db.get(Familia, familia_id)
    url = f"{str(request.base_url).rstrip('/')}/f/{token}"
    mensaje = (f"Hola, {familia.nombre_familia}. La escuela le comparte su enlace personal para revisar "
               f"autorizaciones y registrar a las personas que pueden recoger a sus hijos: {url}")
    return render(request, "admin/enlace.html", {
        "familia": familia,
        "url": url,
        "expira": expira,
        "mensaje": mensaje,
        "whatsapp": _enlace_whatsapp(familia.telefono, mensaje),
    }, usuario=usuario, seccion="familias")


# ---------- Personas autorizadas ----------

@router.get("/adultos", response_class=HTMLResponse)
def adultos(request: Request, estado: str = "pendiente", db: Session = Depends(get_db),
            usuario: UsuarioStaff = Depends(usuario_panel)):
    if estado not in ESTADOS_ADULTO:
        estado = "pendiente"
    filas = db.execute(
        select(AdultoAutorizado, Alumno.nombre, Salon.nombre)
        .join(Alumno, Alumno.id == AdultoAutorizado.alumno_id)
        .join(Salon, Salon.id == Alumno.salon_id)
        .where(AdultoAutorizado.estado == estado)
        .order_by(Salon.nombre, Alumno.nombre, AdultoAutorizado.nombre)
        .limit(500)
    ).all()
    return render(request, "admin/adultos.html", {"filas": filas, "estado": estado, "estados": ESTADOS_ADULTO},
                  usuario=usuario, seccion="adultos")


@router.post("/adultos/{adulto_id}/estado", dependencies=[Depends(verificar_csrf)])
def decidir_adulto(request: Request, adulto_id: int, estado: str = Form(""), volver_a: str = Form("pendiente"),
                   db: Session = Depends(get_db), usuario: UsuarioStaff = Depends(usuario_panel)):
    try:
        adulto = services.decidir_adulto(db, usuario_id=usuario.id, ip=_ip(request), adulto_id=adulto_id,
                                         estado=estado)
        avisar(request, f"{adulto.nombre}: {'validado' if adulto.estado == 'validado' else 'rechazado'}.")
    except services.ErrorNegocio as e:
        avisar(request, e.mensaje, "error")
    destino = volver_a if volver_a in ESTADOS_ADULTO else "pendiente"
    return volver(f"/admin/adultos?estado={destino}")


# ---------- Campañas ----------

@router.get("/campanas", response_class=HTMLResponse)
def campanas(request: Request, db: Session = Depends(get_db), usuario: UsuarioStaff = Depends(usuario_panel)):
    momento = ahora()
    filas = [
        {"campana": c, "vigente": aware(c.fecha_limite) > momento,
         "totales": services.totalizar(services.progreso_campana(db, c.id))}
        for c in db.scalars(select(Campana).order_by(Campana.fecha_limite.desc())).all()
    ]
    return render(request, "admin/campanas.html", {
        "filas": filas,
        "salones": db.scalars(select(Salon).order_by(Salon.nombre)).all(),
    }, usuario=usuario, seccion="campanas")


@router.post("/campanas", dependencies=[Depends(verificar_csrf)])
def crear_campana(request: Request, titulo: str = Form(""), descripcion: str = Form(""), tipo: str = Form(""),
                  texto_legal: str = Form(""), fecha_limite: str = Form(""), salon_ids: list[int] = Form([]),
                  db: Session = Depends(get_db), usuario: UsuarioStaff = Depends(usuario_panel)):
    error = None
    limite = None
    try:
        limite = desde_formulario(fecha_limite)
    except ValueError:
        error = "Indique la fecha y hora límite."
    if not titulo.strip() or len(titulo) > 160:
        error = "Escriba un título de hasta 160 caracteres."
    elif tipo not in TIPOS:
        error = "Elija el tipo de autorización."
    elif not texto_legal.strip():
        error = "Pegue el texto exacto de la autorización."
    elif limite is not None and limite <= ahora():
        error = "La fecha límite debe ser futura."
    if error:
        avisar(request, error, "error")
        return volver("/admin/campanas")
    try:
        campana, _ = services.crear_campana(db, usuario_id=usuario.id, ip=_ip(request), titulo=titulo,
                                            descripcion=descripcion, tipo=tipo, texto_legal=texto_legal,
                                            fecha_limite=limite, salon_ids=salon_ids)
    except services.ErrorNegocio as e:
        avisar(request, e.mensaje, "error")
        return volver("/admin/campanas")
    avisar(request, "Campaña creada. Ya aparece en los enlaces de las familias de esos salones.")
    return volver(f"/admin/campanas/{campana.id}")


def _alumnos_de_campana(db: Session, campana_id: int):
    return db.execute(
        select(Alumno, Salon.nombre, RespuestaAutorizacion)
        .join(Salon, Salon.id == Alumno.salon_id)
        .join(CampanaSalon, (CampanaSalon.salon_id == Alumno.salon_id) & (CampanaSalon.campana_id == campana_id))
        .outerjoin(RespuestaAutorizacion, (RespuestaAutorizacion.alumno_id == Alumno.id)
                   & (RespuestaAutorizacion.campana_id == campana_id))
        .where(Alumno.activo.is_(True))
        .order_by(Salon.nombre, Alumno.nombre)
    ).all()


@router.get("/campanas/{campana_id}", response_class=HTMLResponse)
def ver_campana(request: Request, campana_id: int, estado: str = "", db: Session = Depends(get_db),
                usuario: UsuarioStaff = Depends(usuario_panel)):
    campana = db.get(Campana, campana_id)
    if campana is None:
        return pagina_error(request, "Campaña no encontrada.", 404)
    progreso = services.progreso_campana(db, campana_id)
    filas = _alumnos_de_campana(db, campana_id)
    if estado == "pendiente":
        filas = [f for f in filas if f[2] is None]
    elif estado in ("aceptada", "rechazada"):
        filas = [f for f in filas if f[2] is not None and f[2].decision == estado]
    return render(request, "admin/campana.html", {
        "campana": campana,
        "progreso": progreso,
        "totales": services.totalizar(progreso),
        "filas": filas,
        "estado": estado,
        "vigente": aware(campana.fecha_limite) > ahora(),
    }, usuario=usuario, seccion="campanas")


def _celda_segura(valor) -> str:
    """Evita que Excel interprete como fórmula un texto escrito por una familia (inyección CSV)."""
    texto = "" if valor is None else str(valor)
    return "'" + texto if texto[:1] in ("=", "+", "-", "@", "\t", "\r") else texto


@router.get("/campanas/{campana_id}/csv")
def exportar_campana(campana_id: int, db: Session = Depends(get_db), usuario: UsuarioStaff = Depends(usuario_panel)):
    campana = db.get(Campana, campana_id)
    if campana is None:
        raise HTTPException(status_code=404)
    salida = io.StringIO()
    escritor = csv.writer(salida, delimiter=";")
    escritor.writerow(["salon", "codigo_interno", "alumno", "estado", "firmante", "parentesco",
                       "fecha_hora_ecuador", "hash_texto"])
    for alumno, salon, respuesta in _alumnos_de_campana(db, campana_id):
        escritor.writerow([_celda_segura(v) for v in (
            salon, alumno.codigo_interno, alumno.nombre,
            respuesta.decision if respuesta else "pendiente",
            respuesta.firmante if respuesta else "",
            respuesta.parentesco if respuesta else "",
            local(respuesta.creada_en) if respuesta else "",
            respuesta.texto_hash if respuesta else "",
        )])
    return Response(
        content="﻿" + salida.getvalue(),  # BOM para que Excel reconozca UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="campana-{campana_id}.csv"'},
    )


# ---------- Bitácora ----------

@router.get("/bitacora", response_class=HTMLResponse)
def bitacora(request: Request, db: Session = Depends(get_db), usuario: UsuarioStaff = Depends(usuario_panel)):
    filas = db.execute(
        select(BitacoraAuditoria, UsuarioStaff.nombre)
        .outerjoin(UsuarioStaff, UsuarioStaff.id == BitacoraAuditoria.usuario_id)
        .order_by(BitacoraAuditoria.id.desc()).limit(200)
    ).all()
    return render(request, "admin/bitacora.html", {"filas": filas}, usuario=usuario, seccion="bitacora")
