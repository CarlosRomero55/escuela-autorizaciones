"""API JSON de administración. La lógica vive en app.services; aquí solo hay validación y permisos."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import services
from app.db import get_db
from app.deps import get_actor
from app.models import AdultoAutorizado, Alumno, Campana, CampanaSalon, Familia, Salon
from app.permisos import Actor, alumnos_visibles, puede, salones_visibles
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


def autor_de(actor: Actor, request: Request) -> services.Autor:
    return services.Autor(usuario_id=actor.id, rol=actor.rol, ip=request.client.host if request.client else None)


def exigir(permiso: str):
    def _dependencia(actor: Actor = Depends(get_actor)) -> Actor:
        if not puede(actor, permiso):
            raise HTTPException(status_code=403, detail="No tiene permiso para esta acción.")
        return actor
    return _dependencia


# Utilidades usadas por el router de portería, que escribe directamente en la sesión
def _flush(db: Session) -> None:
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=services.DUPLICADO) from None


def _guardar(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=services.DUPLICADO) from None


# ---------- Salones y alumnos (alcance según el rol) ----------

@router.get("/salones", response_model=list[SalonOut])
def listar_salones(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return db.scalars(select(Salon).where(Salon.id.in_(salones_visibles(db, actor))).order_by(Salon.nombre)).all()


@router.post("/salones", response_model=SalonOut, status_code=201)
def crear_salon(body: SalonIn, request: Request, db: Session = Depends(get_db),
                actor: Actor = Depends(exigir("estructura.gestionar"))):
    return services.crear_salon(db, autor_de(actor, request), body.grado_id, body.paralelo_id, body.tutor_id)


@router.get("/alumnos", response_model=list[AlumnoOut])
def listar_alumnos(salon_id: int | None = None, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    consulta = select(Alumno).where(Alumno.id.in_(alumnos_visibles(db, actor))).order_by(Alumno.nombre)
    if salon_id is not None:
        consulta = consulta.where(Alumno.salon_id == salon_id)
    return db.scalars(consulta).all()


@router.post("/alumnos", response_model=AlumnoOut, status_code=201)
def crear_alumno(body: AlumnoIn, request: Request, db: Session = Depends(get_db),
                 actor: Actor = Depends(exigir("alumnos.gestionar"))):
    return services.crear_alumno(db, autor_de(actor, request), **body.model_dump())


@router.patch("/alumnos/{alumno_id}", response_model=AlumnoOut)
def actualizar_alumno(alumno_id: int, body: AlumnoPatch, request: Request, db: Session = Depends(get_db),
                      actor: Actor = Depends(exigir("alumnos.gestionar"))):
    if body.activo is None:
        alumno = db.get(Alumno, alumno_id)
        if alumno is None:
            raise HTTPException(status_code=404, detail="Alumno no encontrado.")
        return alumno
    return services.cambiar_activo_alumno(db, autor_de(actor, request), alumno_id, body.activo)


# ---------- Familias y personas autorizadas ----------

GESTION = "autorizaciones.gestionar"


@router.get("/familias", response_model=list[FamiliaOut])
def listar_familias(db: Session = Depends(get_db), _: Actor = Depends(exigir(GESTION))):
    return db.scalars(select(Familia).order_by(Familia.nombre_familia)).all()


@router.post("/familias", response_model=FamiliaOut, status_code=201)
def crear_familia(body: FamiliaIn, request: Request, db: Session = Depends(get_db),
                  actor: Actor = Depends(exigir(GESTION))):
    return services.crear_familia(db, autor_de(actor, request), **body.model_dump())


@router.get("/alumnos/{alumno_id}/adultos", response_model=list[AdultoOut])
def listar_adultos(alumno_id: int, db: Session = Depends(get_db), _: Actor = Depends(exigir(GESTION))):
    if db.get(Alumno, alumno_id) is None:
        raise HTTPException(status_code=404, detail="Alumno no encontrado.")
    consulta = select(AdultoAutorizado).where(AdultoAutorizado.alumno_id == alumno_id).order_by(AdultoAutorizado.nombre)
    return db.scalars(consulta).all()


@router.post("/alumnos/{alumno_id}/adultos", response_model=AdultoOut, status_code=201)
def agregar_adulto(alumno_id: int, body: AdultoIn, request: Request, db: Session = Depends(get_db),
                   actor: Actor = Depends(exigir(GESTION))):
    return services.agregar_adulto(db, autor_de(actor, request), alumno_id=alumno_id, **body.model_dump())


@router.patch("/adultos/{adulto_id}/estado", response_model=AdultoOut)
def decidir_adulto(adulto_id: int, body: EstadoAdultoIn, request: Request, db: Session = Depends(get_db),
                   actor: Actor = Depends(exigir(GESTION))):
    return services.decidir_adulto(db, autor_de(actor, request), adulto_id, body.estado)


# ---------- Campañas de autorización ----------

def _campana_out(campana: Campana, salon_ids: list[int]) -> CampanaOut:
    return CampanaOut(id=campana.id, titulo=campana.titulo, tipo=campana.tipo,
                      fecha_limite=campana.fecha_limite, salon_ids=salon_ids)


@router.get("/campanas", response_model=list[CampanaOut])
def listar_campanas(db: Session = Depends(get_db), _: Actor = Depends(exigir(GESTION))):
    resultado = []
    for campana in db.scalars(select(Campana).order_by(Campana.fecha_limite)).all():
        salones = db.scalars(select(CampanaSalon.salon_id).where(CampanaSalon.campana_id == campana.id)).all()
        resultado.append(_campana_out(campana, list(salones)))
    return resultado


@router.post("/campanas", response_model=CampanaOut, status_code=201)
def crear_campana(body: CampanaIn, request: Request, db: Session = Depends(get_db),
                  actor: Actor = Depends(exigir(GESTION))):
    campana, salon_ids = services.crear_campana(db, autor_de(actor, request), **body.model_dump())
    return _campana_out(campana, salon_ids)


@router.get("/campanas/{campana_id}/progreso")
def progreso(campana_id: int, db: Session = Depends(get_db), _: Actor = Depends(exigir(GESTION))):
    if db.get(Campana, campana_id) is None:
        raise HTTPException(status_code=404, detail="Campaña no encontrada.")
    filas = services.progreso_campana(db, campana_id)
    return {"salones": filas, "totales": services.totalizar(filas)}
