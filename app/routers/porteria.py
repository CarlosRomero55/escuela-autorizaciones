from datetime import UTC, datetime, time, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import security
from app.audit import registrar
from app.db import get_db
from app.deps import require_roles
from app.models import (
    AdultoAutorizado,
    Alumno,
    AnioLectivo,
    CodigoRecogida,
    Incidente,
    Recogida,
    Salon,
    Usuario,
)
from app.routers.admin import _flush, _guardar
from app.routers.familias import (
    ENLACE_NO_VALIDO,
    _ahora,
    _alumno_de_familia,
    _aware,
    _error,
    _familia_del_enlace,
    _ip,
    _local,
    templates,
)
from app.tiempo import TZ_ESCUELA

# Quienes atienden la portería
PORTERIA = ("administrador", "coordinacion", "porteria")
# Quienes gestionan autorizaciones temporales
GESTION = ("administrador", "coordinacion")

PASOS_NO_AUTORIZADO = [
    "No entregue al alumno. Manténgalo en un lugar supervisado, cerca de personal de la escuela.",
    "Pida a la persona su documento de identidad y anote su nombre completo.",
    "Comuníquese de inmediato con la coordinación o con dirección, usando el número de contacto de la escuela.",
    "Si la persona insiste, se pone agresiva o hay riesgo para el alumno, llame a emergencias.",
    "Registre el incidente en esta pantalla con los hechos observados, sin juzgar a la persona.",
]

api = APIRouter(prefix="/api", tags=["portería"])
publico_codigos = APIRouter(prefix="/f", include_in_schema=False)


class ValidarCodigoIn(BaseModel):
    alumno_id: int
    codigo: str = Field(pattern=r"^\d{6}$")
    sync_id: str | None = Field(default=None, max_length=64)


class RecogidaManualIn(BaseModel):
    alumno_id: int
    adulto_id: int
    sync_id: str | None = Field(default=None, max_length=64)
    # Hora del evento según el teléfono, y si el envío se demoró por falta de red
    ocurrida_en: datetime | None = None
    diferido: bool = False


class IncidenteIn(BaseModel):
    alumno_id: int
    descripcion: str = Field(min_length=10, max_length=2000)
    personas_involucradas: str | None = Field(default=None, max_length=1000)
    acciones_tomadas: str | None = Field(default=None, max_length=2000)
    sync_id: str | None = Field(default=None, max_length=64)
    ocurrida_en: datetime | None = None
    diferido: bool = False


class TemporalIn(BaseModel):
    nombre: str = Field(min_length=3, max_length=120)
    parentesco: str = Field(min_length=2, max_length=40)
    telefono: str | None = Field(default=None, max_length=20)
    vigente_desde: datetime
    vigente_hasta: datetime
    canal: str = Field(min_length=3, max_length=40)
    evidencia: str = Field(min_length=5, max_length=2000)
    autorizado_por: str = Field(min_length=3, max_length=120)


def _vigente(adulto: AdultoAutorizado, ahora: datetime) -> bool:
    if adulto.estado != "validado":
        return False
    if _aware(adulto.vigente_desde) > ahora:
        return False
    return adulto.vigente_hasta is None or _aware(adulto.vigente_hasta) > ahora


def _alumno_activo(db: Session, alumno_id: int) -> Alumno:
    alumno = db.get(Alumno, alumno_id)
    if alumno is None or not alumno.activo:
        raise HTTPException(status_code=404, detail="Alumno no encontrado.")
    return alumno


def _recogida_out(recogida: Recogida, alerta: str | None = None) -> dict:
    return {
        "id": recogida.id,
        "alumno_id": recogida.alumno_id,
        "adulto_id": recogida.adulto_id,
        "no_autorizado": recogida.no_autorizado,
        "ocurrida_en": _aware(recogida.ocurrida_en).isoformat(),
        "sin_conexion": recogida.sin_conexion,
        "alerta": alerta,
    }


def _momento_del_evento(ocurrida_en: datetime | None, diferido: bool, request: Request) -> tuple[datetime, bool]:
    """Hora del evento y si llegó tarde desde la cola sin conexión. Se rechazan horas imposibles."""
    ahora = _ahora()
    if ocurrida_en is None:
        return ahora, False
    settings = request.app.state.settings
    momento = _aware(ocurrida_en).astimezone(UTC)
    if momento > ahora + timedelta(minutes=5):
        raise HTTPException(status_code=422, detail="La hora del registro está en el futuro. Revise la hora del teléfono.")
    if momento < ahora - timedelta(hours=settings.porteria_desfase_maximo_horas):
        raise HTTPException(status_code=422, detail=(
            f"El registro tiene más de {settings.porteria_desfase_maximo_horas} horas. "
            "Repórtelo a coordinación para registrarlo manualmente."))
    return min(momento, ahora), diferido


def _inicio_del_dia() -> datetime:
    hoy = _ahora().astimezone(TZ_ESCUELA).date()
    return datetime.combine(hoy, time.min, tzinfo=TZ_ESCUELA).astimezone(UTC)


# ---------- Búsqueda y consulta ----------

@api.get("/porteria/alumnos")
def buscar_alumnos(
    q: str | None = None,
    salon_id: int | None = None,
    db: Session = Depends(get_db),
    _: Usuario = Depends(require_roles(*PORTERIA, "docente")),
):
    consulta = select(Alumno, Salon.nombre).join(Salon, Salon.id == Alumno.salon_id).where(Alumno.activo.is_(True))
    if q and q.strip():
        consulta = consulta.where(Alumno.nombre.ilike(f"%{q.strip()[:60]}%"))
    if salon_id is not None:
        consulta = consulta.where(Alumno.salon_id == salon_id)
    filas = db.execute(consulta.order_by(Alumno.nombre).limit(50)).all()
    return [{"id": a.id, "nombre": a.nombre, "salon": salon} for a, salon in filas]


@api.get("/porteria/alumnos/{alumno_id}/adultos")
def adultos_autorizados(
    alumno_id: int,
    db: Session = Depends(get_db),
    _: Usuario = Depends(require_roles(*PORTERIA, "docente")),
):
    """Solo personas validadas y vigentes. No se devuelven teléfonos ni documentos."""
    alumno = _alumno_activo(db, alumno_id)
    ahora = _ahora()
    adultos = db.scalars(select(AdultoAutorizado).where(AdultoAutorizado.alumno_id == alumno.id)).all()
    return [
        {"id": a.id, "nombre": a.nombre, "parentesco": a.parentesco, "temporal": a.temporal}
        for a in adultos
        if _vigente(a, ahora)
    ]


@api.get("/porteria/padron")
def padron(
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(require_roles(*PORTERIA)),
):
    """Lo mínimo para consultar sin conexión: alumnos activos, su salón y las personas autorizadas
    vigentes (o que lo serán antes de que el padrón venza). Sin teléfonos, documentos ni fotos."""
    settings = request.app.state.settings
    ahora = _ahora()
    vence = ahora + timedelta(hours=settings.porteria_padron_horas)
    anio = db.scalar(select(AnioLectivo).where(AnioLectivo.activo.is_(True)))
    salones = db.scalars(select(Salon).where(Salon.anio_lectivo_id == (anio.id if anio else -1))
                         .order_by(Salon.nombre)).all()
    alumnos = db.scalars(select(Alumno).where(Alumno.activo.is_(True),
                                              Alumno.salon_id.in_([s.id for s in salones]))
                         .order_by(Alumno.nombre)).all()
    adultos: dict[int, list[dict]] = {}
    for a in db.scalars(select(AdultoAutorizado).where(
            AdultoAutorizado.estado == "validado", AdultoAutorizado.alumno_id.in_([x.id for x in alumnos]))):
        if _aware(a.vigente_desde) >= vence or (a.vigente_hasta is not None and _aware(a.vigente_hasta) <= ahora):
            continue
        adultos.setdefault(a.alumno_id, []).append({
            "id": a.id, "nombre": a.nombre, "parentesco": a.parentesco, "temporal": a.temporal,
            "desde": _aware(a.vigente_desde).isoformat(),
            "hasta": _aware(a.vigente_hasta).isoformat() if a.vigente_hasta else None,
        })
    salidas = {r.alumno_id: _aware(r.ocurrida_en).isoformat() for r in db.scalars(
        select(Recogida).where(Recogida.ocurrida_en >= _inicio_del_dia(), Recogida.no_autorizado.is_(False))
        .order_by(Recogida.ocurrida_en))}
    nombres_salon = {s.id: s.nombre for s in salones}
    registrar(db, usuario_id=usuario.id, accion="descargar_padron", entidad="porteria",
              cambio={"alumnos": len(alumnos)}, ip=_ip(request))
    db.commit()
    return {
        "generado_en": ahora.isoformat(),
        "vence_en": vence.isoformat(),
        "salones": [{"id": s.id, "nombre": s.nombre} for s in salones],
        "alumnos": [{
            "id": a.id, "nombre": a.nombre, "codigo": a.codigo_interno, "salon_id": a.salon_id,
            "salon": nombres_salon.get(a.salon_id, ""), "adultos": adultos.get(a.id, []),
            "salida": salidas.get(a.id),
        } for a in alumnos],
    }


@api.get("/porteria/salidas-hoy")
def salidas_hoy(
    db: Session = Depends(get_db),
    _: Usuario = Depends(require_roles(*PORTERIA)),
):
    filas = db.execute(
        select(Recogida, Alumno, Salon.nombre, AdultoAutorizado.nombre, AdultoAutorizado.parentesco)
        .join(Alumno, Alumno.id == Recogida.alumno_id)
        .join(Salon, Salon.id == Alumno.salon_id, isouter=True)
        .join(AdultoAutorizado, AdultoAutorizado.id == Recogida.adulto_id, isouter=True)
        .where(Recogida.ocurrida_en >= _inicio_del_dia())
        .order_by(Recogida.ocurrida_en.desc()).limit(200)
    ).all()
    return [{
        "id": r.id, "hora": _aware(r.ocurrida_en).isoformat(), "alumno": a.nombre, "salon": salon or "",
        "adulto": adulto, "parentesco": parentesco, "no_autorizado": r.no_autorizado, "sin_conexion": r.sin_conexion,
    } for r, a, salon, adulto, parentesco in filas]


# ---------- Registro de recogidas ----------

@api.post("/porteria/recogidas/validar-codigo", status_code=201)
def validar_codigo(
    body: ValidarCodigoIn,
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(require_roles(*PORTERIA)),
):
    if body.sync_id:
        # Reintento de sincronización offline: devolver el registro existente, no duplicarlo
        existente = db.scalar(select(Recogida).where(Recogida.sync_id == body.sync_id))
        if existente is not None:
            return _recogida_out(existente)

    alumno = _alumno_activo(db, body.alumno_id)
    settings = request.app.state.settings
    ahora = _ahora()
    codigos = db.scalars(select(CodigoRecogida).where(
        CodigoRecogida.alumno_id == alumno.id, CodigoRecogida.usado_en.is_(None))).all()
    if not codigos:
        raise HTTPException(status_code=404, detail="No hay un código vigente para este alumno.")

    usables = [
        c for c in codigos
        if security.is_pickup_code_usable(
            expires_at=_aware(c.expira_en), used_at=None, attempts=c.intentos,
            now=ahora, max_attempts=settings.pickup_code_max_attempts,
        )
    ]
    if not usables:
        raise HTTPException(status_code=409, detail="El código expiró o agotó sus intentos. Pida a la familia uno nuevo.")

    for codigo in usables:
        if security.secrets_match(codigo.codigo_hash, body.codigo, settings.token_hmac_key):
            adulto = db.get(AdultoAutorizado, codigo.adulto_id)
            if adulto is None or not _vigente(adulto, ahora):
                raise HTTPException(status_code=409, detail="Esta persona ya no está autorizada para recoger al alumno.")
            codigo.usado_en = ahora
            recogida = Recogida(
                alumno_id=alumno.id,
                adulto_id=adulto.id,
                no_autorizado=False,
                registrada_por=usuario.id,
                verificada_por=usuario.id,
                ocurrida_en=ahora,
                sync_id=body.sync_id,
            )
            db.add(recogida)
            _flush(db)
            registrar(db, usuario_id=usuario.id, accion="recogida_por_codigo", entidad="recogida",
                      entidad_id=recogida.id, cambio={"alumno_id": alumno.id, "adulto_id": adulto.id},
                      ip=_ip(request))
            _guardar(db)
            return _recogida_out(recogida)

    # Ningún código coincidió: cada intento cuenta contra los códigos vigentes del alumno
    for codigo in usables:
        codigo.intentos += 1
    registrar(db, usuario_id=usuario.id, accion="codigo_incorrecto", entidad="alumno", entidad_id=alumno.id,
              ip=_ip(request))
    _guardar(db)
    raise HTTPException(status_code=422, detail="Código incorrecto.")


@api.post("/porteria/recogidas", status_code=201)
def registrar_recogida_manual(
    body: RecogidaManualIn,
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(require_roles(*PORTERIA)),
):
    """Recogida de una persona autorizada y vigente, verificada por personal de portería sin código."""
    if body.sync_id:
        existente = db.scalar(select(Recogida).where(Recogida.sync_id == body.sync_id))
        if existente is not None:
            return _recogida_out(existente)

    alumno = _alumno_activo(db, body.alumno_id)
    adulto = db.get(AdultoAutorizado, body.adulto_id)
    momento, sin_conexion = _momento_del_evento(body.ocurrida_en, body.diferido, request)
    if adulto is None or adulto.alumno_id != alumno.id:
        raise HTTPException(status_code=409, detail="Esta persona no está autorizada para recoger al alumno.")
    vigente = _vigente(adulto, momento)
    if not vigente and not sin_conexion:
        raise HTTPException(status_code=409, detail="Esta persona no está autorizada para recoger al alumno.")

    recogida = Recogida(
        alumno_id=alumno.id, adulto_id=adulto.id, no_autorizado=False, registrada_por=usuario.id,
        verificada_por=usuario.id, ocurrida_en=momento, sync_id=body.sync_id, sin_conexion=sin_conexion,
    )
    db.add(recogida)
    _flush(db)
    alerta = None
    if not vigente:
        # La entrega ya ocurrió con el padrón guardado en el teléfono, pero la autorización cambió
        # mientras no había red. Se registra igual (es un hecho) y queda un incidente para coordinación.
        alerta = ("La autorización de esta persona cambió mientras el teléfono estaba sin conexión. "
                  "Se registró un incidente para que coordinación lo revise.")
        db.add(Incidente(recogida_id=recogida.id, registrado_por=usuario.id, descripcion=(
            "Recogida registrada sin conexión con una persona cuya autorización no estaba vigente "
            "al sincronizar. Revisar con la familia.")))
    registrar(db, usuario_id=usuario.id, accion="recogida_manual", entidad="recogida", entidad_id=recogida.id,
              cambio={"alumno_id": alumno.id, "adulto_id": adulto.id, "sin_conexion": sin_conexion,
                      "vigente": vigente}, ip=_ip(request))
    _guardar(db)
    return _recogida_out(recogida, alerta)


# ---------- Persona no autorizada ----------

@api.post("/porteria/recogidas/no-autorizada", status_code=201)
def registrar_no_autorizado(
    body: IncidenteIn,
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(require_roles(*PORTERIA)),
):
    """Registra el intento y el incidente en una sola transacción, y devuelve el protocolo a seguir."""
    if body.sync_id:
        existente = db.scalar(select(Recogida).where(Recogida.sync_id == body.sync_id))
        if existente is not None:
            return {"recogida_id": existente.id, "pasos": PASOS_NO_AUTORIZADO}

    alumno = _alumno_activo(db, body.alumno_id)
    momento, sin_conexion = _momento_del_evento(body.ocurrida_en, body.diferido, request)
    recogida = Recogida(
        alumno_id=alumno.id, adulto_id=None, no_autorizado=True, registrada_por=usuario.id,
        verificada_por=usuario.id, ocurrida_en=momento, sync_id=body.sync_id, sin_conexion=sin_conexion,
    )
    db.add(recogida)
    _flush(db)
    incidente = Incidente(
        recogida_id=recogida.id,
        descripcion=body.descripcion.strip(),
        personas_involucradas=body.personas_involucradas,
        acciones_tomadas=body.acciones_tomadas,
        registrado_por=usuario.id,
    )
    db.add(incidente)
    _flush(db)
    registrar(db, usuario_id=usuario.id, accion="incidente_no_autorizado", entidad="incidente",
              entidad_id=incidente.id, cambio={"alumno_id": alumno.id, "recogida_id": recogida.id},
              ip=_ip(request))
    _guardar(db)
    return {"recogida_id": recogida.id, "incidente_id": incidente.id, "pasos": PASOS_NO_AUTORIZADO}


# ---------- Autorizaciones temporales ----------

@api.post("/alumnos/{alumno_id}/temporales", status_code=201)
def autorizar_temporal(
    alumno_id: int,
    body: TemporalIn,
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(require_roles(*GESTION)),
):
    alumno = _alumno_activo(db, alumno_id)
    # Se guarda todo en UTC para que la comparación de vigencia sea correcta en cualquier base
    desde = _aware(body.vigente_desde).astimezone(UTC)
    hasta = _aware(body.vigente_hasta).astimezone(UTC)
    if hasta <= desde:
        raise HTTPException(status_code=422, detail="La vigencia final debe ser posterior a la inicial.")

    adulto = AdultoAutorizado(
        alumno_id=alumno.id,
        nombre=body.nombre,
        parentesco=body.parentesco,
        telefono=body.telefono,
        estado="validado",
        temporal=True,
        vigente_desde=desde,
        vigente_hasta=hasta,
        evidencia=f"Canal: {body.canal}. Evidencia: {body.evidencia}. Autorizado por: {body.autorizado_por}.",
        validado_por=usuario.id,
    )
    db.add(adulto)
    _flush(db)
    registrar(db, usuario_id=usuario.id, accion="autorizar_temporal", entidad="adulto_autorizado",
              entidad_id=adulto.id, cambio={"alumno_id": alumno.id, "canal": body.canal,
                                           "vigente_hasta": body.vigente_hasta.isoformat()},
              ip=_ip(request))
    _guardar(db)
    return {"id": adulto.id, "vigente_hasta": body.vigente_hasta.isoformat()}


# ---------- Código de recogida generado por la familia ----------

@publico_codigos.post("/{token}/codigos", response_class=HTMLResponse)
def generar_codigo(
    token: str,
    request: Request,
    alumno_id: int = Form(...),
    adulto_id: int = Form(...),
    db: Session = Depends(get_db),
):
    familia = _familia_del_enlace(request, db, token)
    if familia is None:
        return _error(request, ENLACE_NO_VALIDO, 404)
    alumno = _alumno_de_familia(db, familia, alumno_id)
    adulto = db.get(AdultoAutorizado, adulto_id)
    ahora = _ahora()
    if alumno is None or adulto is None or adulto.alumno_id != alumno.id or not _vigente(adulto, ahora):
        return _error(request, "Esta persona no está autorizada para recoger a este alumno.", 409)

    settings = request.app.state.settings
    # Un solo código vigente por persona y alumno: el nuevo invalida los anteriores
    db.execute(update(CodigoRecogida).where(
        CodigoRecogida.adulto_id == adulto.id,
        CodigoRecogida.alumno_id == alumno.id,
        CodigoRecogida.usado_en.is_(None),
    ).values(usado_en=ahora))

    codigo = security.generate_pickup_code()
    expira = security.pickup_code_expiry(ahora, settings.pickup_code_ttl_minutes)
    db.add(CodigoRecogida(
        adulto_id=adulto.id,
        alumno_id=alumno.id,
        codigo_hash=security.hash_secret(codigo, settings.token_hmac_key),
        expira_en=expira,
    ))
    registrar(db, usuario_id=None, accion="generar_codigo", entidad="adulto_autorizado", entidad_id=adulto.id,
              cambio={"alumno_id": alumno.id}, ip=_ip(request))
    db.commit()
    return templates.TemplateResponse(request, "familia/codigo.html", {
        "codigo": codigo,
        "adulto": adulto,
        "alumno": alumno,
        "expira": _local(expira),
        "minutos": settings.pickup_code_ttl_minutes,
    })
