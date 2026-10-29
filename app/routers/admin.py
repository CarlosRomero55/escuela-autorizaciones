import hashlib

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import registrar
from app.db import get_db
from app.deps import get_current_user, require_roles
from app.models import (
    Alumno,
    AdultoAutorizado,
    Campana,
    CampanaSalon,
    Familia,
    Salon,
    UsuarioStaff,
)
from app.schemas import (
    AdultoIn,
    AdultoOut,
    AlumnoIn,
    AlumnoOut,
    AlumnoPatch,
    CampanaIn,
    CampanaOut,
    EstadoAdultoIn,
    FamiliaIn,
    FamiliaOut,
    SalonIn,
    SalonOut,
)

router = APIRouter(prefix="/api", tags=["administración"])

# Quienes pueden crear y modificar registros
GESTION = ("administrador", "coordinadora")
# Quienes pueden consultar alumnos (portería y maestras necesitan ver a los alumnos)
LECTURA = ("administrador", "coordinadora", "maestra", "porteria")


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


DUPLICADO = "Ya existe un registro con esos datos."


def _flush(db: Session) -> None:
    """Envía los cambios pendientes a la base para obtener ids; convierte duplicados en 409."""
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=DUPLICADO)


def _guardar(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=DUPLICADO)


def _no_encontrado(nombre: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{nombre} no encontrado.")


# ---------- Salones ----------

@router.get("/salones", response_model=list[SalonOut])
def listar_salones(db: Session = Depends(get_db), _: UsuarioStaff = Depends(get_current_user)):
    return db.scalars(select(Salon).order_by(Salon.nombre)).all()


@router.post("/salones", response_model=SalonOut, status_code=201)
def crear_salon(
    body: SalonIn,
    request: Request,
    db: Session = Depends(get_db),
    usuario: UsuarioStaff = Depends(require_roles(*GESTION)),
):
    salon = Salon(nombre=body.nombre, grado=body.grado)
    db.add(salon)
    _flush(db)
    registrar(db, usuario_id=usuario.id, accion="crear", entidad="salon", entidad_id=salon.id,
              cambio=body.model_dump(), ip=_ip(request))
    _guardar(db)
    return salon


# ---------- Alumnos ----------

@router.get("/alumnos", response_model=list[AlumnoOut])
def listar_alumnos(
    salon_id: int | None = None,
    db: Session = Depends(get_db),
    _: UsuarioStaff = Depends(require_roles(*LECTURA)),
):
    consulta = select(Alumno).order_by(Alumno.nombre)
    if salon_id is not None:
        consulta = consulta.where(Alumno.salon_id == salon_id)
    return db.scalars(consulta).all()


@router.post("/alumnos", response_model=AlumnoOut, status_code=201)
def crear_alumno(
    body: AlumnoIn,
    request: Request,
    db: Session = Depends(get_db),
    usuario: UsuarioStaff = Depends(require_roles(*GESTION)),
):
    if db.get(Salon, body.salon_id) is None:
        raise _no_encontrado("Salón")
    alumno = Alumno(codigo_interno=body.codigo_interno, nombre=body.nombre, salon_id=body.salon_id)
    db.add(alumno)
    _flush(db)
    registrar(db, usuario_id=usuario.id, accion="crear", entidad="alumno", entidad_id=alumno.id,
              cambio=body.model_dump(), ip=_ip(request))
    _guardar(db)
    return alumno


@router.patch("/alumnos/{alumno_id}", response_model=AlumnoOut)
def actualizar_alumno(
    alumno_id: int,
    body: AlumnoPatch,
    request: Request,
    db: Session = Depends(get_db),
    usuario: UsuarioStaff = Depends(require_roles(*GESTION)),
):
    alumno = db.get(Alumno, alumno_id)
    if alumno is None:
        raise _no_encontrado("Alumno")
    cambios = body.model_dump(exclude_none=True)
    for campo, valor in cambios.items():
        setattr(alumno, campo, valor)
    registrar(db, usuario_id=usuario.id, accion="actualizar", entidad="alumno", entidad_id=alumno.id,
              cambio=cambios, ip=_ip(request))
    _guardar(db)
    return alumno


# ---------- Familias ----------

@router.get("/familias", response_model=list[FamiliaOut])
def listar_familias(db: Session = Depends(get_db), _: UsuarioStaff = Depends(require_roles(*GESTION))):
    return db.scalars(select(Familia).order_by(Familia.nombre_familia)).all()


@router.post("/familias", response_model=FamiliaOut, status_code=201)
def crear_familia(
    body: FamiliaIn,
    request: Request,
    db: Session = Depends(get_db),
    usuario: UsuarioStaff = Depends(require_roles(*GESTION)),
):
    familia = Familia(**body.model_dump())
    db.add(familia)
    _flush(db)
    registrar(db, usuario_id=usuario.id, accion="crear", entidad="familia", entidad_id=familia.id,
              cambio={"nombre_familia": body.nombre_familia, "consentimiento_mensajes": body.consentimiento_mensajes},
              ip=_ip(request))
    _guardar(db)
    return familia


# ---------- Adultos autorizados ----------

@router.get("/alumnos/{alumno_id}/adultos", response_model=list[AdultoOut])
def listar_adultos(
    alumno_id: int,
    db: Session = Depends(get_db),
    _: UsuarioStaff = Depends(require_roles(*GESTION)),
):
    if db.get(Alumno, alumno_id) is None:
        raise _no_encontrado("Alumno")
    consulta = select(AdultoAutorizado).where(AdultoAutorizado.alumno_id == alumno_id).order_by(AdultoAutorizado.nombre)
    return db.scalars(consulta).all()


@router.post("/alumnos/{alumno_id}/adultos", response_model=AdultoOut, status_code=201)
def agregar_adulto(
    alumno_id: int,
    body: AdultoIn,
    request: Request,
    db: Session = Depends(get_db),
    usuario: UsuarioStaff = Depends(require_roles(*GESTION)),
):
    if db.get(Alumno, alumno_id) is None:
        raise _no_encontrado("Alumno")
    if body.familia_id is not None and db.get(Familia, body.familia_id) is None:
        raise _no_encontrado("Familia")
    adulto = AdultoAutorizado(
        alumno_id=alumno_id,
        familia_id=body.familia_id,
        nombre=body.nombre,
        parentesco=body.parentesco,
        telefono=body.telefono,
        estado="pendiente",
    )
    db.add(adulto)
    _flush(db)
    registrar(db, usuario_id=usuario.id, accion="crear", entidad="adulto_autorizado", entidad_id=adulto.id,
              cambio={"alumno_id": alumno_id, "parentesco": body.parentesco}, ip=_ip(request))
    _guardar(db)
    return adulto


@router.patch("/adultos/{adulto_id}/estado", response_model=AdultoOut)
def decidir_adulto(
    adulto_id: int,
    body: EstadoAdultoIn,
    request: Request,
    db: Session = Depends(get_db),
    usuario: UsuarioStaff = Depends(require_roles(*GESTION)),
):
    adulto = db.get(AdultoAutorizado, adulto_id)
    if adulto is None:
        raise _no_encontrado("Adulto")
    anterior = adulto.estado
    adulto.estado = body.estado
    adulto.validado_por = usuario.id
    registrar(db, usuario_id=usuario.id, accion="validar" if body.estado == "validado" else "rechazar",
              entidad="adulto_autorizado", entidad_id=adulto.id,
              cambio={"de": anterior, "a": body.estado}, ip=_ip(request))
    _guardar(db)
    return adulto


# ---------- Campañas de autorización ----------

def _campana_out(campana: Campana, salon_ids: list[int]) -> CampanaOut:
    return CampanaOut(
        id=campana.id,
        titulo=campana.titulo,
        tipo=campana.tipo,
        fecha_limite=campana.fecha_limite,
        salon_ids=salon_ids,
    )


@router.get("/campanas", response_model=list[CampanaOut])
def listar_campanas(db: Session = Depends(get_db), _: UsuarioStaff = Depends(require_roles(*GESTION))):
    campanas = db.scalars(select(Campana).order_by(Campana.fecha_limite)).all()
    resultado = []
    for campana in campanas:
        salones = db.scalars(select(CampanaSalon.salon_id).where(CampanaSalon.campana_id == campana.id)).all()
        resultado.append(_campana_out(campana, list(salones)))
    return resultado


@router.post("/campanas", response_model=CampanaOut, status_code=201)
def crear_campana(
    body: CampanaIn,
    request: Request,
    db: Session = Depends(get_db),
    usuario: UsuarioStaff = Depends(require_roles(*GESTION)),
):
    salon_ids = sorted(set(body.salon_ids))
    encontrados = db.scalars(select(Salon.id).where(Salon.id.in_(salon_ids))).all()
    if len(encontrados) != len(salon_ids):
        raise _no_encontrado("Uno de los salones")

    campana = Campana(
        titulo=body.titulo,
        descripcion=body.descripcion,
        tipo=body.tipo,
        texto_legal=body.texto_legal,
        # El hash fija el texto exacto que se mostrará a las familias
        texto_hash=hashlib.sha256(body.texto_legal.encode("utf-8")).hexdigest(),
        fecha_limite=body.fecha_limite,
        creado_por=usuario.id,
    )
    db.add(campana)
    _flush(db)
    for salon_id in salon_ids:
        db.add(CampanaSalon(campana_id=campana.id, salon_id=salon_id))
    registrar(db, usuario_id=usuario.id, accion="crear", entidad="campana", entidad_id=campana.id,
              cambio={"titulo": body.titulo, "tipo": body.tipo, "salon_ids": salon_ids,
                      "texto_hash": campana.texto_hash},
              ip=_ip(request))
    _guardar(db)
    return _campana_out(campana, salon_ids)
