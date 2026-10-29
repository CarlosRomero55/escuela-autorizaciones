from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import crypto, security, services
from app.audit import registrar
from app.db import get_db
from app.models import (
    AdultoAutorizado,
    Alumno,
    Campana,
    CampanaSalon,
    Familia,
    FamiliaAlumno,
    RespuestaAutorizacion,
    TokenAcceso,
)
from app.permisos import Actor
from app.routers.admin import GESTION, autor_de, exigir
from app.tiempo import ahora as _ahora
from app.tiempo import aware as _aware
from app.tiempo import local as _local
from app.web import templates

MAX_ADULTOS_POR_ALUMNO = 2

# Rutas de administración (JSON, con control de rol)
api = APIRouter(prefix="/api", tags=["familias"])
# Rutas públicas para familias (HTML, acceso mediante enlace firmado)
publico = APIRouter(prefix="/f", tags=["familias (público)"], include_in_schema=False)


class VinculoIn(BaseModel):
    alumno_id: int


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


_texto_hash = services.hash_texto


def _error(request: Request, mensaje: str, status: int) -> HTMLResponse:
    return templates.TemplateResponse(request, "familia/error.html", {"mensaje": mensaje}, status_code=status)


# ---------- Administración ----------

@api.post("/familias/{familia_id}/alumnos", status_code=201)
def vincular_alumno(
    familia_id: int,
    body: VinculoIn,
    request: Request,
    db: Session = Depends(get_db),
    actor: Actor = Depends(exigir(GESTION)),
):
    services.vincular_alumno(db, autor_de(actor, request), familia_id, body.alumno_id)
    return {"familia_id": familia_id, "alumno_id": body.alumno_id}


@api.post("/familias/{familia_id}/enlace")
def generar_enlace(
    familia_id: int,
    request: Request,
    db: Session = Depends(get_db),
    actor: Actor = Depends(exigir(GESTION)),
):
    """Genera un enlace nuevo. El token se muestra una sola vez; los enlaces anteriores quedan invalidados."""
    token, expira = services.generar_enlace(db, request.app.state.settings, autor_de(actor, request), familia_id)
    return {"url": f"/f/{token}", "expira_en": expira.isoformat()}


# ---------- Acceso de familias ----------

def _familia_del_enlace(request: Request, db: Session, token: str) -> Familia | None:
    settings = request.app.state.settings
    token_hash = security.hash_secret(token, settings.token_hmac_key)
    registro = db.scalar(select(TokenAcceso).where(TokenAcceso.token_hash == token_hash))
    if registro is None or _aware(registro.expira_en) <= _ahora():
        return None
    return db.get(Familia, registro.familia_id)


def _alumno_de_familia(db: Session, familia: Familia, alumno_id: int) -> Alumno | None:
    if db.get(FamiliaAlumno, (familia.id, alumno_id)) is None:
        return None
    return db.get(Alumno, alumno_id)


def _campana_aplica(db: Session, campana: Campana, alumno: Alumno) -> bool:
    return db.get(CampanaSalon, (campana.id, alumno.salon_id)) is not None


ENLACE_NO_VALIDO = "Este enlace no es válido o ya expiró. Pida a la escuela un enlace nuevo."


@publico.get("/{token}", response_class=HTMLResponse)
def inicio(token: str, request: Request, db: Session = Depends(get_db)):
    familia = _familia_del_enlace(request, db, token)
    if familia is None:
        return _error(request, ENLACE_NO_VALIDO, 404)

    ahora = _ahora()
    alumnos = db.scalars(
        select(Alumno)
        .join(FamiliaAlumno, FamiliaAlumno.alumno_id == Alumno.id)
        .where(FamiliaAlumno.familia_id == familia.id)
        .order_by(Alumno.nombre)
    ).all()

    bloques = []
    for alumno in alumnos:
        campanas = db.scalars(
            select(Campana)
            .join(CampanaSalon, CampanaSalon.campana_id == Campana.id)
            .where(CampanaSalon.salon_id == alumno.salon_id)
            .order_by(Campana.fecha_limite)
        ).all()
        respondidas = set(db.scalars(
            select(RespuestaAutorizacion.campana_id).where(RespuestaAutorizacion.alumno_id == alumno.id)
        ).all())
        adultos = db.scalars(
            select(AdultoAutorizado).where(AdultoAutorizado.alumno_id == alumno.id).order_by(AdultoAutorizado.nombre)
        ).all()
        bloques.append({
            "alumno": alumno,
            "campanas": [
                {
                    "campana": c,
                    "limite": _local(c.fecha_limite),
                    "respondida": c.id in respondidas,
                    "vencida": _aware(c.fecha_limite) <= ahora,
                }
                for c in campanas
            ],
            "adultos": adultos,
            "puede_agregar_adulto": len(adultos) < MAX_ADULTOS_POR_ALUMNO,
        })

    return templates.TemplateResponse(
        request,
        "familia/inicio.html",
        {"token": token, "familia": familia, "bloques": bloques, "max_adultos": MAX_ADULTOS_POR_ALUMNO},
    )


@publico.get("/{token}/campanas/{campana_id}", response_class=HTMLResponse)
def ver_autorizacion(token: str, campana_id: int, alumno_id: int, request: Request, db: Session = Depends(get_db)):
    familia = _familia_del_enlace(request, db, token)
    if familia is None:
        return _error(request, ENLACE_NO_VALIDO, 404)
    alumno = _alumno_de_familia(db, familia, alumno_id)
    campana = db.get(Campana, campana_id)
    if alumno is None or campana is None or not _campana_aplica(db, campana, alumno):
        return _error(request, "Esta autorización no corresponde a este alumno.", 404)

    respuesta = db.scalar(select(RespuestaAutorizacion).where(
        RespuestaAutorizacion.campana_id == campana.id, RespuestaAutorizacion.alumno_id == alumno.id))
    if respuesta is not None:
        return RedirectResponse(f"/f/{token}/confirmacion/{respuesta.id}", status_code=303)
    if _aware(campana.fecha_limite) <= _ahora():
        return _error(request, "El plazo para responder a esta autorización ya venció.", 409)

    return templates.TemplateResponse(request, "familia/autorizacion.html", {
        "token": token,
        "alumno": alumno,
        "campana": campana,
        "limite": _local(campana.fecha_limite),
        "texto_hash": _texto_hash(campana.texto_legal),
    })


@publico.post("/{token}/campanas/{campana_id}")
def enviar_autorizacion(
    token: str,
    campana_id: int,
    request: Request,
    alumno_id: int = Form(...),
    firmante: str = Form(..., min_length=3, max_length=120),
    parentesco: str = Form(..., min_length=2, max_length=40),
    decision: str = Form(...),
    texto_hash: str = Form(..., min_length=64, max_length=64),
    db: Session = Depends(get_db),
):
    familia = _familia_del_enlace(request, db, token)
    if familia is None:
        return _error(request, ENLACE_NO_VALIDO, 404)
    alumno = _alumno_de_familia(db, familia, alumno_id)
    campana = db.get(Campana, campana_id)
    if alumno is None or campana is None or not _campana_aplica(db, campana, alumno):
        return _error(request, "Esta autorización no corresponde a este alumno.", 404)
    if decision not in ("aceptada", "rechazada"):
        return _error(request, "Debe marcar aceptar o rechazar.", 422)
    if _aware(campana.fecha_limite) <= _ahora():
        return _error(request, "El plazo para responder a esta autorización ya venció.", 409)

    # La familia firmó el texto que vio. Si la escuela cambió el texto, debe volver a leerlo.
    if texto_hash != _texto_hash(campana.texto_legal):
        return _error(request, "El texto de la autorización cambió. Abra el enlace de nuevo y vuelva a leerlo.", 409)

    respuesta = RespuestaAutorizacion(
        campana_id=campana.id,
        alumno_id=alumno.id,
        familia_id=familia.id,
        firmante=firmante.strip(),
        parentesco=parentesco.strip(),
        decision=decision,
        canal="enlace",
        ip=_ip(request),
        user_agent=request.headers.get("user-agent", "")[:300],
        texto_hash=texto_hash,
    )
    db.add(respuesta)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return _error(request, "Ya se registró una respuesta para este alumno en esta autorización.", 409)

    registrar(db, usuario_id=None, accion="autorizar" if decision == "aceptada" else "rechazar",
              entidad="respuesta_autorizacion", entidad_id=respuesta.id,
              cambio={"campana_id": campana.id, "alumno_id": alumno.id, "texto_hash": texto_hash},
              ip=_ip(request))
    db.commit()
    return RedirectResponse(f"/f/{token}/confirmacion/{respuesta.id}", status_code=303)


@publico.get("/{token}/confirmacion/{respuesta_id}", response_class=HTMLResponse)
def confirmacion(token: str, respuesta_id: int, request: Request, db: Session = Depends(get_db)):
    familia = _familia_del_enlace(request, db, token)
    if familia is None:
        return _error(request, ENLACE_NO_VALIDO, 404)
    respuesta = db.get(RespuestaAutorizacion, respuesta_id)
    if respuesta is None or respuesta.familia_id != familia.id:
        return _error(request, "Comprobante no encontrado.", 404)
    return templates.TemplateResponse(request, "familia/confirmacion.html", {
        "respuesta": respuesta,
        "alumno": db.get(Alumno, respuesta.alumno_id),
        "campana": db.get(Campana, respuesta.campana_id),
        "fecha": _local(respuesta.creada_en),
    })


@publico.post("/{token}/adultos")
def registrar_adulto(
    token: str,
    request: Request,
    alumno_id: int = Form(...),
    nombre: str = Form(..., min_length=3, max_length=120),
    parentesco: str = Form(..., min_length=2, max_length=40),
    telefono: str = Form("", max_length=20),
    documento: str = Form("", max_length=20),
    db: Session = Depends(get_db),
):
    familia = _familia_del_enlace(request, db, token)
    if familia is None:
        return _error(request, ENLACE_NO_VALIDO, 404)
    alumno = _alumno_de_familia(db, familia, alumno_id)
    if alumno is None:
        return _error(request, "Este alumno no está vinculado a su familia.", 404)

    existentes = db.scalars(select(AdultoAutorizado).where(
        AdultoAutorizado.alumno_id == alumno.id, AdultoAutorizado.estado != "rechazado")).all()
    if len(existentes) >= MAX_ADULTOS_POR_ALUMNO:
        return _error(request, f"Ya hay {MAX_ADULTOS_POR_ALUMNO} adultos registrados para este alumno.", 409)

    settings = request.app.state.settings
    documento = documento.strip()
    adulto = AdultoAutorizado(
        alumno_id=alumno.id,
        familia_id=familia.id,
        nombre=nombre.strip(),
        parentesco=parentesco.strip(),
        telefono=telefono.strip() or None,
        # El documento se cifra y se guarda además un HMAC para buscarlo sin descifrar.
        documento_cifrado=crypto.cifrar(documento, settings.encryption_key) if documento else None,
        documento_hash=security.hash_secret(documento, settings.token_hmac_key) if documento else None,
        estado="pendiente",
    )
    db.add(adulto)
    db.flush()
    # La bitácora no guarda el documento ni otros datos personales, solo el hecho y el alumno
    registrar(db, usuario_id=None, accion="registrar_adulto", entidad="adulto_autorizado", entidad_id=adulto.id,
              cambio={"alumno_id": alumno.id, "parentesco": adulto.parentesco}, ip=_ip(request))
    db.commit()
    return RedirectResponse(f"/f/{token}", status_code=303)
