"""API JSON de administración. La lógica vive en app.services; aquí solo hay validación y permisos."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import services
from app.db import get_db
from app.deps import get_current_user, require_roles
from app.models import AdultoAutorizado, Alumno, Campana, CampanaSalon, Familia, Salon, UsuarioStaff
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


# Utilidades usadas por los routers de portería, que aún escriben directamente en la sesión
def _flush(db: Session) -> None:
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=services.DUPLICADO)


def _guardar(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=services.DUPLICADO)


# ---------- Salones ----------

@router.get("/salones", response_model=list[SalonOut])
def listar_salones(db: Session = Depends(get_db), _: UsuarioStaff = Depends(get_current_user)):
    return db.scalars(select(Salon).order_by(Salon.nombre)).all()


@router.post("/salones", response_model=SalonOut, status_code=201)
def crear_salon(body: SalonIn, request: Request, db: Session = Depends(get_db),
                usuario: UsuarioStaff = Depends(require_roles(*GESTION))):
    return services.crear_salon(db, usuario_id=usuario.id, ip=_ip(request), nombre=body.nombre, grado=body.grado)


# ---------- Alumnos ----------

@router.get("/alumnos", response_model=list[AlumnoOut])
def listar_alumnos(salon_id: int | None = None, db: Session = Depends(get_db),
                   _: UsuarioStaff = Depends(require_roles(*LECTURA))):
    consulta = select(Alumno).order_by(Alumno.nombre)
    if salon_id is not None:
        consulta = consulta.where(Alumno.salon_id == salon_id)
    return db.scalars(consulta).all()


@router.post("/alumnos", response_model=AlumnoOut, status_code=201)
def crear_alumno(body: AlumnoIn, request: Request, db: Session = Depends(get_db),
                 usuario: UsuarioStaff = Depends(require_roles(*GESTION))):
    return services.crear_alumno(db, usuario_id=usuario.id, ip=_ip(request), codigo_interno=body.codigo_interno,
                                 nombre=body.nombre, salon_id=body.salon_id)


@router.patch("/alumnos/{alumno_id}", response_model=AlumnoOut)
def actualizar_alumno(alumno_id: int, body: AlumnoPatch, request: Request, db: Session = Depends(get_db),
                      usuario: UsuarioStaff = Depends(require_roles(*GESTION))):
    if body.activo is None:
        alumno = db.get(Alumno, alumno_id)
        if alumno is None:
            raise HTTPException(status_code=404, detail="Alumno no encontrado.")
        return alumno
    return services.cambiar_activo_alumno(db, usuario_id=usuario.id, ip=_ip(request), alumno_id=alumno_id,
                                          activo=body.activo)


# ---------- Familias ----------

@router.get("/familias", response_model=list[FamiliaOut])
def listar_familias(db: Session = Depends(get_db), _: UsuarioStaff = Depends(require_roles(*GESTION))):
    return db.scalars(select(Familia).order_by(Familia.nombre_familia)).all()


@router.post("/familias", response_model=FamiliaOut, status_code=201)
def crear_familia(body: FamiliaIn, request: Request, db: Session = Depends(get_db),
                  usuario: UsuarioStaff = Depends(require_roles(*GESTION))):
    return services.crear_familia(db, usuario_id=usuario.id, ip=_ip(request), **body.model_dump())


# ---------- Adultos autorizados ----------

@router.get("/alumnos/{alumno_id}/adultos", response_model=list[AdultoOut])
def listar_adultos(alumno_id: int, db: Session = Depends(get_db),
                   _: UsuarioStaff = Depends(require_roles(*GESTION))):
    if db.get(Alumno, alumno_id) is None:
        raise HTTPException(status_code=404, detail="Alumno no encontrado.")
    consulta = select(AdultoAutorizado).where(AdultoAutorizado.alumno_id == alumno_id).order_by(AdultoAutorizado.nombre)
    return db.scalars(consulta).all()


@router.post("/alumnos/{alumno_id}/adultos", response_model=AdultoOut, status_code=201)
def agregar_adulto(alumno_id: int, body: AdultoIn, request: Request, db: Session = Depends(get_db),
                   usuario: UsuarioStaff = Depends(require_roles(*GESTION))):
    return services.agregar_adulto(db, usuario_id=usuario.id, ip=_ip(request), alumno_id=alumno_id,
                                   **body.model_dump())


@router.patch("/adultos/{adulto_id}/estado", response_model=AdultoOut)
def decidir_adulto(adulto_id: int, body: EstadoAdultoIn, request: Request, db: Session = Depends(get_db),
                   usuario: UsuarioStaff = Depends(require_roles(*GESTION))):
    return services.decidir_adulto(db, usuario_id=usuario.id, ip=_ip(request), adulto_id=adulto_id,
                                   estado=body.estado)


# ---------- Campañas de autorización ----------

def _campana_out(campana: Campana, salon_ids: list[int]) -> CampanaOut:
    return CampanaOut(id=campana.id, titulo=campana.titulo, tipo=campana.tipo,
                      fecha_limite=campana.fecha_limite, salon_ids=salon_ids)


@router.get("/campanas", response_model=list[CampanaOut])
def listar_campanas(db: Session = Depends(get_db), _: UsuarioStaff = Depends(require_roles(*GESTION))):
    campanas = db.scalars(select(Campana).order_by(Campana.fecha_limite)).all()
    resultado = []
    for campana in campanas:
        salones = db.scalars(select(CampanaSalon.salon_id).where(CampanaSalon.campana_id == campana.id)).all()
        resultado.append(_campana_out(campana, list(salones)))
    return resultado


@router.post("/campanas", response_model=CampanaOut, status_code=201)
def crear_campana(body: CampanaIn, request: Request, db: Session = Depends(get_db),
                  usuario: UsuarioStaff = Depends(require_roles(*GESTION))):
    campana, salon_ids = services.crear_campana(
        db, usuario_id=usuario.id, ip=_ip(request), titulo=body.titulo, descripcion=body.descripcion,
        tipo=body.tipo, texto_legal=body.texto_legal, fecha_limite=body.fecha_limite, salon_ids=body.salon_ids,
    )
    return _campana_out(campana, salon_ids)


@router.get("/campanas/{campana_id}/progreso")
def progreso(campana_id: int, db: Session = Depends(get_db), _: UsuarioStaff = Depends(require_roles(*GESTION))):
    if db.get(Campana, campana_id) is None:
        raise HTTPException(status_code=404, detail="Campaña no encontrada.")
    filas = services.progreso_campana(db, campana_id)
    return {"salones": filas, "totales": services.totalizar(filas)}
