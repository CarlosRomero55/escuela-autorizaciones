"""Motor de permisos.

Dos niveles:
1. **Permisos de función** (qué pantallas y acciones tiene un rol): `puede(actor, "usuarios.gestionar")`.
   Si falla, la respuesta es 403, porque la existencia de una pantalla de gestión no es secreta.
2. **Alcance sobre los datos** (qué alumnos y salones puede ver según su relación real con ellos):
   `alumnos_visibles()` y `salones_visibles()` devuelven subconsultas que se aplican en cada consulta.
   Fuera de alcance la respuesta es 404, idéntica a la de un registro inexistente, para no revelar
   que el recurso existe.

Los permisos se evalúan con el **rol activo** de la sesión, no con la unión de todos los roles del
usuario: un docente que además es representante ve a sus hijos solo cuando actúa como representante.
Así cada acción queda auditada con un único rol y se aplica el mínimo privilegio.
"""

from dataclasses import dataclass, field

from sqlalchemy import Select, false, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Alumno,
    AnioLectivo,
    Materia,
    Matricula,
    RepresentanteAlumno,
    Salon,
    SalonMateriaDocente,
    Usuario,
    UsuarioRol,
)
from app.tiempo import ahora, aware

# Orden en que se elige el rol activo inicial cuando una persona tiene varios
PRIORIDAD_ROLES = (
    "administrador", "coordinacion", "directivo", "docente", "consejeria",
    "auditor", "porteria", "representante", "estudiante",
)

ETIQUETAS_ROL = {
    "administrador": "Administración",
    "coordinacion": "Coordinación",
    "directivo": "Directivo",
    "docente": "Docente",
    "consejeria": "Consejería",
    "auditor": "Auditoría",
    "porteria": "Portería",
    "representante": "Representante",
    "estudiante": "Estudiante",
}

# Roles con segundo factor obligatorio: manejan datos de muchos alumnos o datos sensibles
ROLES_TOTP_OBLIGATORIO = frozenset({"administrador", "coordinacion", "directivo", "docente", "consejeria", "auditor"})

PERMISOS: dict[str, frozenset[str]] = {
    "administrador": frozenset({
        "usuarios.gestionar", "estructura.gestionar", "alumnos.gestionar", "matriculas.gestionar",
        "representantes.gestionar", "autorizaciones.gestionar", "porteria.operar", "bitacora.ver",
        "envios.ver",
    }),
    "coordinacion": frozenset({"autorizaciones.gestionar", "porteria.operar"}),
    "directivo": frozenset(),
    "docente": frozenset(),
    "consejeria": frozenset(),
    "auditor": frozenset({"bitacora.ver"}),
    "porteria": frozenset({"porteria.operar"}),
    "representante": frozenset(),
    "estudiante": frozenset(),
}


class SinAlcance(Exception):
    """El recurso no existe o está fuera del alcance del usuario (se responde 404 en ambos casos)."""


@dataclass
class Actor:
    usuario: Usuario
    rol: str
    roles: tuple[str, ...]
    # Áreas de un directivo; None significa toda la institución
    areas: frozenset[int] | None = field(default=None)

    @property
    def id(self) -> int:
        return self.usuario.id


def roles_vigentes(usuario: Usuario) -> list[UsuarioRol]:
    momento = ahora()
    return [r for r in usuario.roles if r.vigente_hasta is None or aware(r.vigente_hasta) > momento]


def construir_actor(usuario: Usuario, rol_activo: str | None) -> Actor | None:
    vigentes = roles_vigentes(usuario)
    nombres = tuple(sorted({r.rol for r in vigentes}, key=PRIORIDAD_ROLES.index))
    if not nombres:
        return None
    rol = rol_activo if rol_activo in nombres else nombres[0]
    areas = None
    if rol == "directivo":
        filas = [r.area_id for r in vigentes if r.rol == "directivo"]
        areas = None if any(a is None for a in filas) else frozenset(filas)
    return Actor(usuario=usuario, rol=rol, roles=nombres, areas=areas)


def requiere_totp(usuario: Usuario) -> bool:
    return any(r.rol in ROLES_TOTP_OBLIGATORIO for r in roles_vigentes(usuario))


def puede(actor: Actor, permiso: str) -> bool:
    return permiso in PERMISOS.get(actor.rol, frozenset())


# ---------- Alcance sobre los datos ----------

def anio_activo(db: Session) -> AnioLectivo | None:
    return db.scalar(select(AnioLectivo).where(AnioLectivo.activo.is_(True)))


def _salones_del_anio(db: Session) -> Select:
    anio = anio_activo(db)
    if anio is None:
        return select(Salon.id).where(false())
    return select(Salon.id).where(Salon.anio_lectivo_id == anio.id)


def _representados(actor: Actor) -> Select:
    return select(RepresentanteAlumno.alumno_id).where(
        RepresentanteAlumno.usuario_id == actor.id,
        RepresentanteAlumno.validado_en.is_not(None),
    )


def salones_visibles(db: Session, actor: Actor) -> Select:
    """Subconsulta con los ids de salones (del año activo) que el actor puede ver."""
    del_anio = _salones_del_anio(db)
    if actor.rol in ("administrador", "coordinacion"):
        return del_anio
    if actor.rol == "docente":
        return select(Salon.id).where(
            Salon.id.in_(del_anio),
            or_(Salon.tutor_id == actor.id,
                Salon.id.in_(select(SalonMateriaDocente.salon_id).where(SalonMateriaDocente.docente_id == actor.id))),
        )
    if actor.rol == "directivo":
        if actor.areas is None:
            return del_anio
        return (select(SalonMateriaDocente.salon_id)
                .join(Materia, Materia.id == SalonMateriaDocente.materia_id)
                .where(Materia.area_id.in_(actor.areas), SalonMateriaDocente.salon_id.in_(del_anio)))
    if actor.rol == "estudiante":
        return (select(Matricula.salon_id).join(Alumno, Alumno.id == Matricula.alumno_id)
                .where(Alumno.persona_id == actor.usuario.persona_id, Matricula.estado == "activa",
                       Matricula.salon_id.in_(del_anio)))
    if actor.rol == "representante":
        return select(Matricula.salon_id).where(
            Matricula.alumno_id.in_(_representados(actor)), Matricula.estado == "activa",
            Matricula.salon_id.in_(del_anio))
    # Consejería, auditoría y portería no ven salones en el portal (sus casos llegan por otros módulos)
    return select(Salon.id).where(false())


def alumnos_visibles(db: Session, actor: Actor) -> Select:
    """Subconsulta con los ids de alumnos que el actor puede ver según su relación real con ellos."""
    if actor.rol in ("administrador", "coordinacion"):
        return select(Alumno.id)
    if actor.rol in ("docente", "directivo"):
        return select(Matricula.alumno_id).where(
            Matricula.estado == "activa", Matricula.salon_id.in_(salones_visibles(db, actor)))
    if actor.rol == "estudiante":
        return select(Alumno.id).where(Alumno.persona_id == actor.usuario.persona_id)
    if actor.rol == "representante":
        return _representados(actor)
    return select(Alumno.id).where(false())


def exigir_alumno(db: Session, actor: Actor, alumno_id: int) -> Alumno:
    alumno = db.scalar(select(Alumno).where(Alumno.id == alumno_id, Alumno.id.in_(alumnos_visibles(db, actor))))
    if alumno is None:
        raise SinAlcance()
    return alumno


def exigir_salon(db: Session, actor: Actor, salon_id: int) -> Salon:
    salon = db.scalar(select(Salon).where(Salon.id == salon_id, Salon.id.in_(salones_visibles(db, actor))))
    if salon is None:
        raise SinAlcance()
    return salon


def puede_ver_alumnos_del_salon(actor: Actor) -> bool:
    """Los compañeros aparecen solo por nombre para estudiantes (decisión de la dirección);
    los representantes no ven el listado del salón."""
    return actor.rol in ("administrador", "coordinacion", "directivo", "docente", "estudiante")
