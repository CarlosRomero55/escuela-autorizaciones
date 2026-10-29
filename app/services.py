"""Operaciones de negocio compartidas por la API JSON y el panel HTML.

Cada operación valida, escribe en la bitácora y confirma la transacción. Los errores se
expresan con excepciones propias; la API las convierte en respuestas JSON y el panel en avisos.
"""

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import security
from app.audit import registrar
from app.config import Settings
from app.models import (
    AdultoAutorizado,
    Alumno,
    Campana,
    CampanaSalon,
    Familia,
    FamiliaAlumno,
    RespuestaAutorizacion,
    Salon,
    TokenAcceso,
)
from app.tiempo import a_utc, ahora

DUPLICADO = "Ya existe un registro con esos datos."


class ErrorNegocio(Exception):
    status_code = 400

    def __init__(self, mensaje: str):
        super().__init__(mensaje)
        self.mensaje = mensaje


class NoEncontrado(ErrorNegocio):
    status_code = 404


class Conflicto(ErrorNegocio):
    status_code = 409


def flush(db: Session) -> None:
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise Conflicto(DUPLICADO)


def commit(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise Conflicto(DUPLICADO)


def hash_texto(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


# ---------- Catálogos ----------

def crear_salon(db: Session, *, usuario_id: int, ip: str | None, nombre: str, grado: str) -> Salon:
    salon = Salon(nombre=nombre.strip(), grado=grado.strip())
    db.add(salon)
    flush(db)
    registrar(db, usuario_id=usuario_id, accion="crear", entidad="salon", entidad_id=salon.id,
              cambio={"nombre": salon.nombre, "grado": salon.grado}, ip=ip)
    commit(db)
    return salon


def crear_alumno(db: Session, *, usuario_id: int, ip: str | None, codigo_interno: str, nombre: str,
                 salon_id: int) -> Alumno:
    if db.get(Salon, salon_id) is None:
        raise NoEncontrado("Salón no encontrado.")
    alumno = Alumno(codigo_interno=codigo_interno.strip(), nombre=nombre.strip(), salon_id=salon_id)
    db.add(alumno)
    flush(db)
    registrar(db, usuario_id=usuario_id, accion="crear", entidad="alumno", entidad_id=alumno.id,
              cambio={"codigo_interno": alumno.codigo_interno, "salon_id": salon_id}, ip=ip)
    commit(db)
    return alumno


def cambiar_activo_alumno(db: Session, *, usuario_id: int, ip: str | None, alumno_id: int, activo: bool) -> Alumno:
    alumno = db.get(Alumno, alumno_id)
    if alumno is None:
        raise NoEncontrado("Alumno no encontrado.")
    alumno.activo = activo
    registrar(db, usuario_id=usuario_id, accion="actualizar", entidad="alumno", entidad_id=alumno.id,
              cambio={"activo": activo}, ip=ip)
    commit(db)
    return alumno


def crear_familia(db: Session, *, usuario_id: int, ip: str | None, nombre_familia: str,
                  telefono: str | None, email: str | None, consentimiento_mensajes: bool) -> Familia:
    familia = Familia(
        nombre_familia=nombre_familia.strip(),
        telefono=(telefono or "").strip() or None,
        email=(email or "").strip() or None,
        consentimiento_mensajes=consentimiento_mensajes,
    )
    db.add(familia)
    flush(db)
    # La bitácora no guarda teléfono ni correo
    registrar(db, usuario_id=usuario_id, accion="crear", entidad="familia", entidad_id=familia.id,
              cambio={"nombre_familia": familia.nombre_familia,
                      "consentimiento_mensajes": consentimiento_mensajes}, ip=ip)
    commit(db)
    return familia


def vincular_alumno(db: Session, *, usuario_id: int, ip: str | None, familia_id: int, alumno_id: int) -> None:
    if db.get(Familia, familia_id) is None or db.get(Alumno, alumno_id) is None:
        raise NoEncontrado("Familia o alumno no encontrado.")
    if db.get(FamiliaAlumno, (familia_id, alumno_id)) is None:
        db.add(FamiliaAlumno(familia_id=familia_id, alumno_id=alumno_id))
    registrar(db, usuario_id=usuario_id, accion="vincular_alumno", entidad="familia", entidad_id=familia_id,
              cambio={"alumno_id": alumno_id}, ip=ip)
    commit(db)


def generar_enlace(db: Session, *, settings: Settings, usuario_id: int, ip: str | None,
                   familia_id: int) -> tuple[str, datetime]:
    """Devuelve el token en claro (solo esta vez) y su vencimiento. Invalida los enlaces anteriores."""
    if db.get(Familia, familia_id) is None:
        raise NoEncontrado("Familia no encontrada.")
    momento = ahora()
    db.execute(
        update(TokenAcceso)
        .where(TokenAcceso.familia_id == familia_id, TokenAcceso.expira_en > momento)
        .values(expira_en=momento)
    )
    token = security.generate_token()
    expira = momento + timedelta(days=settings.family_link_ttl_days)
    db.add(TokenAcceso(
        familia_id=familia_id,
        token_hash=security.hash_secret(token, settings.token_hmac_key),
        expira_en=expira,
    ))
    registrar(db, usuario_id=usuario_id, accion="generar_enlace", entidad="familia", entidad_id=familia_id,
              cambio={"expira_en": expira.isoformat()}, ip=ip)
    commit(db)
    return token, expira


# ---------- Adultos autorizados ----------

def agregar_adulto(db: Session, *, usuario_id: int, ip: str | None, alumno_id: int, nombre: str,
                   parentesco: str, telefono: str | None, familia_id: int | None) -> AdultoAutorizado:
    if db.get(Alumno, alumno_id) is None:
        raise NoEncontrado("Alumno no encontrado.")
    if familia_id is not None and db.get(Familia, familia_id) is None:
        raise NoEncontrado("Familia no encontrada.")
    adulto = AdultoAutorizado(
        alumno_id=alumno_id,
        familia_id=familia_id,
        nombre=nombre.strip(),
        parentesco=parentesco.strip(),
        telefono=(telefono or "").strip() or None,
        estado="pendiente",
    )
    db.add(adulto)
    flush(db)
    registrar(db, usuario_id=usuario_id, accion="crear", entidad="adulto_autorizado", entidad_id=adulto.id,
              cambio={"alumno_id": alumno_id, "parentesco": adulto.parentesco}, ip=ip)
    commit(db)
    return adulto


def decidir_adulto(db: Session, *, usuario_id: int, ip: str | None, adulto_id: int, estado: str) -> AdultoAutorizado:
    if estado not in ("validado", "rechazado"):
        raise ErrorNegocio("Estado no válido.")
    adulto = db.get(AdultoAutorizado, adulto_id)
    if adulto is None:
        raise NoEncontrado("Adulto no encontrado.")
    anterior = adulto.estado
    adulto.estado = estado
    adulto.validado_por = usuario_id
    registrar(db, usuario_id=usuario_id, accion="validar" if estado == "validado" else "rechazar",
              entidad="adulto_autorizado", entidad_id=adulto.id, cambio={"de": anterior, "a": estado}, ip=ip)
    commit(db)
    return adulto


# ---------- Campañas ----------

def crear_campana(db: Session, *, usuario_id: int, ip: str | None, titulo: str, descripcion: str | None,
                  tipo: str, texto_legal: str, fecha_limite: datetime, salon_ids: list[int]) -> tuple[Campana, list[int]]:
    salon_ids = sorted(set(salon_ids))
    if not salon_ids:
        raise ErrorNegocio("Elija al menos un salón.")
    encontrados = db.scalars(select(Salon.id).where(Salon.id.in_(salon_ids))).all()
    if len(encontrados) != len(salon_ids):
        raise NoEncontrado("Uno de los salones no existe.")
    if not texto_legal.strip():
        raise ErrorNegocio("El texto de la autorización es obligatorio.")

    campana = Campana(
        titulo=titulo.strip(),
        descripcion=(descripcion or "").strip() or None,
        tipo=tipo,
        texto_legal=texto_legal,
        # El hash fija el texto exacto que verán las familias
        texto_hash=hash_texto(texto_legal),
        fecha_limite=a_utc(fecha_limite),
        creado_por=usuario_id,
    )
    db.add(campana)
    flush(db)
    for salon_id in salon_ids:
        db.add(CampanaSalon(campana_id=campana.id, salon_id=salon_id))
    registrar(db, usuario_id=usuario_id, accion="crear", entidad="campana", entidad_id=campana.id,
              cambio={"titulo": campana.titulo, "tipo": tipo, "salon_ids": salon_ids,
                      "texto_hash": campana.texto_hash}, ip=ip)
    commit(db)
    return campana, salon_ids


def progreso_campana(db: Session, campana_id: int) -> list[dict]:
    """Avance por salón: alumnos activos, aceptadas, rechazadas y pendientes."""
    salones = db.scalars(
        select(Salon).join(CampanaSalon, CampanaSalon.salon_id == Salon.id)
        .where(CampanaSalon.campana_id == campana_id).order_by(Salon.nombre)
    ).all()
    filas = []
    for salon in salones:
        total = db.scalar(select(func.count()).select_from(Alumno)
                          .where(Alumno.salon_id == salon.id, Alumno.activo.is_(True)))
        conteo = dict(db.execute(
            select(RespuestaAutorizacion.decision, func.count())
            .join(Alumno, Alumno.id == RespuestaAutorizacion.alumno_id)
            .where(RespuestaAutorizacion.campana_id == campana_id,
                   Alumno.salon_id == salon.id, Alumno.activo.is_(True))
            .group_by(RespuestaAutorizacion.decision)
        ).all())
        aceptadas, rechazadas = conteo.get("aceptada", 0), conteo.get("rechazada", 0)
        filas.append({
            "salon_id": salon.id,
            "salon": salon.nombre,
            "total": total,
            "aceptadas": aceptadas,
            "rechazadas": rechazadas,
            "pendientes": max(total - aceptadas - rechazadas, 0),
        })
    return filas


def totalizar(filas: list[dict]) -> dict:
    claves = ("total", "aceptadas", "rechazadas", "pendientes")
    totales = {c: sum(f[c] for f in filas) for c in claves}
    totales["avance"] = round(100 * (totales["aceptadas"] + totales["rechazadas"]) / totales["total"]) if totales["total"] else 0
    return totales


# ---------- Importación de alumnos desde CSV ----------

COLUMNAS_CSV = ("codigo_interno", "nombre", "salon")
MAX_BYTES_CSV = 1_000_000
MAX_FILAS_CSV = 2000


@dataclass
class FilaImportacion:
    linea: int
    codigo_interno: str
    nombre: str
    salon: str
    salon_id: int | None
    estado: str  # valida | duplicada | error
    mensaje: str = ""


def decodificar_csv(contenido: bytes) -> str:
    if len(contenido) > MAX_BYTES_CSV:
        raise ErrorNegocio("El archivo supera 1 MB.")
    try:
        return contenido.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Excel en Windows suele guardar CSV en esta codificación
        return contenido.decode("cp1252")


def analizar_csv(db: Session, texto: str) -> list[FilaImportacion]:
    if not texto.strip():
        raise ErrorNegocio("El archivo está vacío.")
    try:
        dialecto = csv.Sniffer().sniff(texto[:4096], delimiters=",;")
    except csv.Error:
        dialecto = csv.excel
    lector = csv.DictReader(io.StringIO(texto), dialect=dialecto)
    encabezados = [(h or "").strip().lower() for h in (lector.fieldnames or [])]
    faltan = [c for c in COLUMNAS_CSV if c not in encabezados]
    if faltan:
        raise ErrorNegocio(f"Faltan columnas en el archivo: {', '.join(faltan)}. "
                           f"Use los encabezados {', '.join(COLUMNAS_CSV)}.")
    lector.fieldnames = encabezados

    salones = {s.nombre.strip().lower(): s.id for s in db.scalars(select(Salon))}
    existentes = set(db.scalars(select(Alumno.codigo_interno)))
    vistos: set[str] = set()
    filas: list[FilaImportacion] = []

    for linea, registro in enumerate(lector, start=2):
        if len(filas) >= MAX_FILAS_CSV:
            raise ErrorNegocio(f"El archivo tiene más de {MAX_FILAS_CSV} filas.")
        codigo = (registro.get("codigo_interno") or "").strip()
        nombre = (registro.get("nombre") or "").strip()
        salon = (registro.get("salon") or "").strip()
        if not (codigo or nombre or salon):
            continue  # fila vacía
        salon_id = salones.get(salon.lower())
        fila = FilaImportacion(linea, codigo, nombre, salon, salon_id, "valida")
        if not codigo or not nombre or not salon:
            fila.estado, fila.mensaje = "error", "Faltan datos obligatorios."
        elif len(codigo) > 20 or len(nombre) > 120:
            fila.estado, fila.mensaje = "error", "El código o el nombre es demasiado largo."
        elif salon_id is None:
            fila.estado, fila.mensaje = "error", f"El salón «{salon}» no existe."
        elif codigo in vistos:
            fila.estado, fila.mensaje = "duplicada", "Código repetido dentro del archivo."
        elif codigo in existentes:
            fila.estado, fila.mensaje = "duplicada", "Ya existe un alumno con este código."
        if codigo:
            vistos.add(codigo)
        filas.append(fila)
    return filas


def importar_alumnos(db: Session, *, usuario_id: int, ip: str | None, texto: str) -> tuple[int, int]:
    """Crea solo las filas válidas, en una sola transacción. Devuelve (creados, omitidos)."""
    filas = analizar_csv(db, texto)
    validas = [f for f in filas if f.estado == "valida"]
    for fila in validas:
        db.add(Alumno(codigo_interno=fila.codigo_interno, nombre=fila.nombre, salon_id=fila.salon_id))
    flush(db)
    omitidos = len(filas) - len(validas)
    registrar(db, usuario_id=usuario_id, accion="importar", entidad="alumno",
              cambio={"creados": len(validas), "omitidos": omitidos}, ip=ip)
    commit(db)
    return len(validas), omitidos
