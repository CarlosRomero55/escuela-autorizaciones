"""Reglas académicas de la Fase 3: publicaciones, rúbricas, tareas, entregas, prórrogas y calificaciones.

Las funciones reciben ya resuelto el alcance (la clase o tarea pasó por app.permisos) y verifican
las reglas propias de cada acción. Los errores usan las excepciones de app.services.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.archivos import MAX_ARCHIVOS_POR_ENTREGA, TIPOS, Almacen, ArchivoRechazado, validar
from app.models import (
    TIPOS_TAREA,
    Alumno,
    ArchivoEntrega,
    CriterioRubrica,
    Entrega,
    EscalaCalificacion,
    Materia,
    Matricula,
    PeriodoCalificacion,
    Prorroga,
    Publicacion,
    RangoEscala,
    Rubrica,
    Salon,
    SalonMateriaDocente,
    Tarea,
)
from app.permisos import anio_activo
from app.services import Autor, ErrorNegocio, NoEncontrado, _bitacora, _limpio, commit, flush
from app.tiempo import a_utc, ahora, aware

CENTESIMA = Decimal("0.01")


def truncar(valor: Decimal) -> Decimal:
    """El Ministerio trunca (no redondea) a dos decimales."""
    return valor.quantize(CENTESIMA, rounding=ROUND_DOWN)


# ===================== Escalas =====================

def escala_de_clase(db: Session, clase: SalonMateriaDocente) -> EscalaCalificacion | None:
    salon = db.get(Salon, clase.salon_id)
    return salon.grado.nivel.escala


def equivalencia(escala: EscalaCalificacion | None, nota: Decimal | None) -> RangoEscala | None:
    if escala is None or nota is None:
        return None
    return next((r for r in escala.rangos if r.minimo is not None and r.minimo <= nota <= r.maximo), None)


def leer_nota(texto: str) -> Decimal:
    """Acepta «8,75» o «8.75». Valida el rango 1–10 y trunca a dos decimales."""
    try:
        nota = Decimal((texto or "").strip().replace(",", "."))
    except InvalidOperation:
        raise ErrorNegocio("Escriba la nota como número, por ejemplo 8,75.") from None
    if not (Decimal("1") <= nota <= Decimal("10")):
        raise ErrorNegocio("La nota debe estar entre 1,00 y 10,00.")
    return truncar(nota)


# ===================== Periodos =====================

def periodos_del_anio(db: Session) -> list[PeriodoCalificacion]:
    anio = anio_activo(db)
    if anio is None:
        return []
    return list(db.scalars(select(PeriodoCalificacion).where(PeriodoCalificacion.anio_lectivo_id == anio.id)
                           .order_by(PeriodoCalificacion.inicio)))


def periodo_actual(db: Session) -> PeriodoCalificacion | None:
    periodos = periodos_del_anio(db)
    hoy = ahora().date()
    return next((p for p in periodos if p.inicio <= hoy <= p.fin), periodos[-1] if periodos else None)


# ===================== Publicaciones y rúbricas =====================

def publicar_en_muro(db: Session, autor: Autor, clase: SalonMateriaDocente, titulo: str, cuerpo: str) -> Publicacion:
    publicacion = Publicacion(clase_id=clase.id, autor_id=autor.usuario_id, titulo=_limpio(titulo, 160, "Título"),
                              cuerpo=_limpio(cuerpo, 5000, "Mensaje"))
    db.add(publicacion)
    flush(db)
    _bitacora(db, autor, "publicar", "publicacion", publicacion.id, {"clase_id": clase.id})
    commit(db)
    return publicacion


def rubricas_disponibles(db: Session, usuario_id: int, materia_id: int) -> list[Rubrica]:
    """Las propias y las compartidas por otros docentes de la misma materia."""
    return list(db.scalars(select(Rubrica).where(
        Rubrica.materia_id == materia_id,
        (Rubrica.autor_id == usuario_id) | Rubrica.compartida.is_(True),
    ).order_by(Rubrica.titulo)))


def crear_rubrica(db: Session, autor: Autor, materia_id: int, titulo: str, criterios: list[tuple[str, str]],
                  compartida: bool) -> Rubrica:
    if db.get(Materia, materia_id) is None:
        raise NoEncontrado("Materia no encontrada.")
    filas = []
    for descripcion, puntos in criterios:
        if not descripcion.strip() and not puntos.strip():
            continue
        try:
            valor = Decimal(puntos.replace(",", "."))
        except InvalidOperation:
            raise ErrorNegocio("Los puntos de cada criterio deben ser números.") from None
        if valor <= 0 or valor > 100:
            raise ErrorNegocio("Los puntos de cada criterio deben estar entre 0 y 100.")
        filas.append((_limpio(descripcion, 300, "Criterio"), valor))
    if not filas:
        raise ErrorNegocio("Agregue al menos un criterio.")
    rubrica = Rubrica(materia_id=materia_id, autor_id=autor.usuario_id, titulo=_limpio(titulo, 160, "Título"),
                      compartida=compartida,
                      criterios=[CriterioRubrica(descripcion=d, puntos=p, orden=i) for i, (d, p) in enumerate(filas)])
    db.add(rubrica)
    flush(db)
    _bitacora(db, autor, "crear", "rubrica", rubrica.id, {"materia_id": materia_id, "criterios": len(filas)})
    commit(db)
    return rubrica


# ===================== Tareas =====================

def crear_tarea(db: Session, autor: Autor, clase: SalonMateriaDocente, *, titulo: str, instrucciones: str, tipo: str,
                fecha_entrega: datetime, peso: int, tipos_permitidos: list[str], admite_enlace: bool,
                admite_prorroga: bool, periodo_id: int, rubrica_id: int | None, publicar: bool) -> Tarea:
    if tipo not in TIPOS_TAREA:
        raise ErrorNegocio("Elija el tipo de actividad.")
    if not 1 <= peso <= 100:
        raise ErrorNegocio("El peso debe estar entre 1 y 100.")
    tipos = [t for t in dict.fromkeys(tipos_permitidos) if t in TIPOS]
    if not tipos and not admite_enlace:
        raise ErrorNegocio("Permita al menos un tipo de archivo o la entrega por enlace.")
    periodo = db.get(PeriodoCalificacion, periodo_id)
    anio = anio_activo(db)
    if periodo is None or anio is None or periodo.anio_lectivo_id != anio.id:
        raise ErrorNegocio("Elija un periodo del año lectivo activo.")
    if periodo.cerrado:
        raise ErrorNegocio("El periodo está cerrado; no se pueden crear tareas en él.")
    if rubrica_id is not None and rubrica_id not in {r.id for r in rubricas_disponibles(db, autor.usuario_id, clase.materia_id)}:
        raise ErrorNegocio("La rúbrica elegida no está disponible para esta materia.")
    momento = ahora()
    tarea = Tarea(clase_id=clase.id, periodo_id=periodo.id, titulo=_limpio(titulo, 160, "Título"),
                  instrucciones=_limpio(instrucciones, 10000, "Instrucciones"), tipo=tipo,
                  fecha_entrega=a_utc(fecha_entrega), peso=peso, tipos_permitidos=",".join(tipos),
                  admite_enlace=admite_enlace, admite_prorroga=admite_prorroga, rubrica_id=rubrica_id,
                  estado="publicada" if publicar else "borrador", publicada_en=momento if publicar else None,
                  creada_por=autor.usuario_id)
    db.add(tarea)
    flush(db)
    _bitacora(db, autor, "crear", "tarea", tarea.id, {"clase_id": clase.id, "estado": tarea.estado, "peso": peso})
    commit(db)
    return tarea


def publicar_tarea(db: Session, autor: Autor, tarea: Tarea) -> Tarea:
    if tarea.estado == "publicada":
        raise ErrorNegocio("La tarea ya está publicada.")
    tarea.estado, tarea.publicada_en = "publicada", ahora()
    _bitacora(db, autor, "publicar", "tarea", tarea.id)
    commit(db)
    return tarea


def fecha_efectiva(db: Session, tarea: Tarea, alumno_id: int) -> datetime:
    """Fecha de entrega para un alumno: la de la tarea o la de su prórroga aprobada más reciente."""
    prorroga = db.scalar(select(func.max(Prorroga.nueva_fecha)).where(
        Prorroga.tarea_id == tarea.id, Prorroga.alumno_id == alumno_id, Prorroga.estado == "aprobada"))
    return aware(prorroga) if prorroga is not None else aware(tarea.fecha_entrega)


def alumno_en_clase(db: Session, clase: SalonMateriaDocente, alumno_id: int) -> bool:
    return db.scalar(select(Matricula.id).where(Matricula.alumno_id == alumno_id, Matricula.salon_id == clase.salon_id,
                                                Matricula.estado == "activa")) is not None


# ===================== Entregas =====================

_URL = re.compile(r"^https?://[^\s<>\"']{3,490}$")


def entregar(db: Session, autor: Autor, almacen: Almacen, tarea: Tarea, alumno_id: int, *,
             archivos: list[tuple[str, bytes]], enlace: str, comentario: str) -> Entrega:
    if tarea.estado != "publicada":
        raise NoEncontrado("Tarea no encontrada.")
    if not alumno_en_clase(db, tarea.clase, alumno_id):
        raise NoEncontrado("Tarea no encontrada.")
    entrega = db.scalar(select(Entrega).where(Entrega.tarea_id == tarea.id, Entrega.alumno_id == alumno_id))
    if entrega is not None and entrega.estado == "calificada":
        raise ErrorNegocio("Esta entrega ya fue calificada. Si necesita corregirla, pida al docente que la devuelva.")

    enlace = (enlace or "").strip()
    if enlace and (not tarea.admite_enlace or not _URL.match(enlace)):
        raise ErrorNegocio("El enlace debe empezar con http:// o https://." if tarea.admite_enlace
                           else "Esta tarea no admite entregas por enlace.")
    archivos = [(nombre, contenido) for nombre, contenido in archivos if nombre and contenido]
    if len(archivos) > MAX_ARCHIVOS_POR_ENTREGA:
        raise ErrorNegocio(f"Puede subir hasta {MAX_ARCHIVOS_POR_ENTREGA} archivos.")
    if not archivos and not enlace:
        raise ErrorNegocio("Adjunte al menos un archivo o un enlace.")
    try:
        validados = [validar(nombre, contenido, tarea.lista_tipos) for nombre, contenido in archivos]
    except ArchivoRechazado as e:
        raise ErrorNegocio(e.mensaje) from None

    momento = ahora()
    estado = "con_retraso" if momento > fecha_efectiva(db, tarea, alumno_id) else "entregada"
    claves_anteriores = []
    if entrega is None:
        entrega = Entrega(tarea_id=tarea.id, alumno_id=alumno_id, estado=estado, entregada_en=momento)
        db.add(entrega)
    else:
        # Una nueva entrega reemplaza a la anterior (entregada o devuelta para corrección)
        claves_anteriores = [a.clave for a in entrega.archivos]
        for archivo in list(entrega.archivos):
            db.delete(archivo)
        entrega.estado, entrega.entregada_en = estado, momento
        entrega.nota = entrega.nivel = None
    entrega.enlace = enlace or None
    entrega.comentario_alumno = (comentario or "").strip()[:2000] or None
    flush(db)
    nuevas = []
    for archivo in validados:
        clave = almacen.nueva_clave()
        almacen.guardar(clave, archivo.contenido)
        nuevas.append(clave)
        db.add(ArchivoEntrega(entrega_id=entrega.id, nombre=archivo.nombre, clave=clave, tipo=archivo.tipo,
                              mime=archivo.mime, tamano=len(archivo.contenido), sha256=archivo.sha256))
    _bitacora(db, autor, "entregar", "entrega", entrega.id,
              {"tarea_id": tarea.id, "alumno_id": alumno_id, "estado": estado, "archivos": len(validados)})
    try:
        commit(db)
    except Exception:
        for clave in nuevas:
            almacen.borrar(clave)
        raise
    for clave in claves_anteriores:
        almacen.borrar(clave)
    db.refresh(entrega)
    return entrega


def calificar(db: Session, autor: Autor, entrega: Entrega, *, nota: str, nivel: str, comentario: str,
              devolver: bool) -> Entrega:
    tarea = db.get(Tarea, entrega.tarea_id)
    periodo = db.get(PeriodoCalificacion, tarea.periodo_id)
    if periodo.cerrado:
        raise ErrorNegocio("El periodo está cerrado. Para modificar la calificación se necesita autorización del directivo.")
    comentario = (comentario or "").strip()[:2000] or None
    if devolver:
        if not comentario:
            raise ErrorNegocio("Explique qué debe corregir el estudiante.")
        entrega.estado, entrega.nota, entrega.nivel = "devuelta", None, None
    else:
        escala = escala_de_clase(db, tarea.clase)
        if escala is not None and escala.tipo == "cualitativa":
            if nivel not in {r.codigo for r in escala.rangos}:
                raise ErrorNegocio("Elija una valoración de la escala.")
            entrega.nota, entrega.nivel = None, nivel
        else:
            entrega.nota = leer_nota(nota)
            rango = equivalencia(escala, entrega.nota)
            entrega.nivel = rango.codigo if rango else None
        entrega.estado = "calificada"
    entrega.comentario_docente = comentario
    entrega.calificada_por, entrega.calificada_en = autor.usuario_id, ahora()
    _bitacora(db, autor, "devolver" if devolver else "calificar", "entrega", entrega.id,
              {"nota": str(entrega.nota) if entrega.nota is not None else None, "nivel": entrega.nivel})
    commit(db)
    return entrega


# ===================== Prórrogas =====================

def solicitar_prorroga(db: Session, autor: Autor, tarea: Tarea, alumno_id: int, motivo: str,
                       nueva_fecha: datetime) -> Prorroga:
    if tarea.estado != "publicada" or not alumno_en_clase(db, tarea.clase, alumno_id):
        raise NoEncontrado("Tarea no encontrada.")
    if not tarea.admite_prorroga:
        raise ErrorNegocio("Esta tarea no admite prórrogas.")
    entrega = db.scalar(select(Entrega).where(Entrega.tarea_id == tarea.id, Entrega.alumno_id == alumno_id))
    if entrega is not None and entrega.estado == "calificada":
        raise ErrorNegocio("La tarea ya fue calificada.")
    if a_utc(nueva_fecha) <= fecha_efectiva(db, tarea, alumno_id):
        raise ErrorNegocio("La nueva fecha debe ser posterior a la fecha de entrega actual.")
    motivo = _limpio(motivo, 500, "Motivo")
    if len(motivo) < 10:
        raise ErrorNegocio("Explique el motivo con al menos 10 caracteres.")
    prorroga = Prorroga(tarea_id=tarea.id, alumno_id=alumno_id, solicitada_por=autor.usuario_id, motivo=motivo,
                        nueva_fecha=a_utc(nueva_fecha))
    db.add(prorroga)
    try:
        flush(db)
    except ErrorNegocio:
        raise ErrorNegocio("Ya hay una solicitud de prórroga pendiente para esta tarea.") from None
    _bitacora(db, autor, "solicitar_prorroga", "tarea", tarea.id, {"alumno_id": alumno_id})
    commit(db)
    return prorroga


def resolver_prorroga(db: Session, autor: Autor, prorroga: Prorroga, aprobar: bool) -> Prorroga:
    if prorroga.estado != "pendiente":
        raise ErrorNegocio("La solicitud ya fue resuelta.")
    prorroga.estado = "aprobada" if aprobar else "rechazada"
    prorroga.resuelta_por, prorroga.resuelta_en = autor.usuario_id, ahora()
    _bitacora(db, autor, f"prorroga_{prorroga.estado}", "tarea", prorroga.tarea_id, {"alumno_id": prorroga.alumno_id})
    commit(db)
    return prorroga


# ===================== Estado, promedios y matriz =====================

ETIQUETAS_ESTADO = {
    "entregada": ("Entregada", "ok"),
    "con_retraso": ("Entregada con retraso", "alerta"),
    "calificada": ("Calificada", "info"),
    "devuelta": ("Devuelta para corrección", "alerta"),
    "pendiente": ("Pendiente", "neutro"),
    "atrasada": ("No entregada", "error"),
}


def estado_para(entrega: Entrega | None, vence: datetime) -> str:
    if entrega is not None:
        return entrega.estado
    return "atrasada" if ahora() > vence else "pendiente"


def promedio(db: Session, clase: SalonMateriaDocente, alumno_id: int, periodo_id: int) -> Decimal | None:
    """Promedio ponderado por peso de las tareas calificadas del periodo, truncado a dos decimales."""
    filas = db.execute(
        select(Entrega.nota, Tarea.peso).join(Tarea, Tarea.id == Entrega.tarea_id)
        .where(Tarea.clase_id == clase.id, Tarea.periodo_id == periodo_id, Entrega.alumno_id == alumno_id,
               Entrega.estado == "calificada", Entrega.nota.is_not(None))
    ).all()
    if not filas:
        return None
    total_peso = sum(peso for _, peso in filas)
    return truncar(sum(Decimal(nota) * peso for nota, peso in filas) / total_peso)


def alumnos_de_clase(db: Session, clase: SalonMateriaDocente) -> list[Alumno]:
    return list(db.scalars(select(Alumno).join(Matricula, Matricula.alumno_id == Alumno.id).where(
        Matricula.salon_id == clase.salon_id, Matricula.estado == "activa").order_by(Alumno.nombre)))


@dataclass
class Celda:
    estado: str
    entrega: Entrega | None


def matriz(db: Session, clase: SalonMateriaDocente, periodo_id: int | None = None):
    """Alumnos × tareas publicadas con el estado de cada entrega."""
    consulta = select(Tarea).where(Tarea.clase_id == clase.id, Tarea.estado == "publicada")
    if periodo_id is not None:
        consulta = consulta.where(Tarea.periodo_id == periodo_id)
    tareas = list(db.scalars(consulta.order_by(Tarea.fecha_entrega)))
    alumnos = alumnos_de_clase(db, clase)
    entregas = {(e.alumno_id, e.tarea_id): e for e in db.scalars(select(Entrega).where(
        Entrega.tarea_id.in_([t.id for t in tareas])))}
    prorrogas: dict[tuple[int, int], datetime] = {}
    for p in db.scalars(select(Prorroga).where(Prorroga.tarea_id.in_([t.id for t in tareas]),
                                               Prorroga.estado == "aprobada")):
        clave = (p.alumno_id, p.tarea_id)
        prorrogas[clave] = max(prorrogas.get(clave, aware(p.nueva_fecha)), aware(p.nueva_fecha))
    celdas = {}
    for alumno in alumnos:
        for tarea in tareas:
            vence = prorrogas.get((alumno.id, tarea.id), aware(tarea.fecha_entrega))
            entrega = entregas.get((alumno.id, tarea.id))
            celdas[(alumno.id, tarea.id)] = Celda(estado_para(entrega, vence), entrega)
    return alumnos, tareas, celdas


def situacion_alumno(db: Session, alumno: Alumno) -> list[dict]:
    """Tareas publicadas del salón del alumno con su fecha efectiva y estado, ordenadas por vencimiento."""
    if alumno.salon_id is None:
        return []
    tareas = db.scalars(select(Tarea).join(SalonMateriaDocente, SalonMateriaDocente.id == Tarea.clase_id).where(
        SalonMateriaDocente.salon_id == alumno.salon_id, Tarea.estado == "publicada")).all()
    entregas = {e.tarea_id: e for e in db.scalars(select(Entrega).where(
        Entrega.alumno_id == alumno.id, Entrega.tarea_id.in_([t.id for t in tareas])))}
    filas = []
    for tarea in tareas:
        vence = fecha_efectiva(db, tarea, alumno.id)
        entrega = entregas.get(tarea.id)
        filas.append({"tarea": tarea, "vence": vence, "entrega": entrega, "estado": estado_para(entrega, vence)})
    return sorted(filas, key=lambda f: f["vence"])


def resumen_situacion(filas: list[dict]) -> dict:
    hechas = sum(1 for f in filas if f["estado"] in ("entregada", "con_retraso", "calificada"))
    total = len(filas)
    return {
        "pendientes": [f for f in filas if f["estado"] in ("pendiente", "atrasada", "devuelta")],
        "hechas": hechas,
        "total": total,
        "porcentaje": round(100 * hechas / total) if total else 0,
    }
