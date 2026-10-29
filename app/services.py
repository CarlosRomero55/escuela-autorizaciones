"""Operaciones de negocio compartidas por la API JSON y las páginas HTML.

Cada operación valida, escribe en la bitácora y confirma la transacción. Los errores se expresan
con excepciones propias; la API las convierte en JSON y las páginas en avisos.
"""

import csv
import hashlib
import io
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import security, sesiones
from app.audit import registrar
from app.auth import emitir_token_cuenta
from app.config import Settings
from app.models import (
    ROLES,
    AdultoAutorizado,
    Alumno,
    AnioLectivo,
    Area,
    Campana,
    CampanaSalon,
    Familia,
    FamiliaAlumno,
    Grado,
    Materia,
    Matricula,
    Paralelo,
    PeriodoCalificacion,
    Persona,
    RepresentanteAlumno,
    RespuestaAutorizacion,
    Salon,
    SalonMateriaDocente,
    TokenAcceso,
    Usuario,
    UsuarioRol,
)
from app.permisos import anio_activo
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


@dataclass(frozen=True)
class Autor:
    """Quién ejecuta la acción, para la bitácora."""

    usuario_id: int | None
    rol: str | None = None
    ip: str | None = None


def _bitacora(db: Session, autor: Autor, accion: str, entidad: str, entidad_id: int | None = None,
              cambio: dict | None = None, motivo: str | None = None) -> None:
    registrar(db, usuario_id=autor.usuario_id, rol=autor.rol, accion=accion, entidad=entidad,
              entidad_id=entidad_id, cambio=cambio, ip=autor.ip, motivo=motivo)


def flush(db: Session) -> None:
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise Conflicto(DUPLICADO) from None


def commit(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise Conflicto(DUPLICADO) from None


def hash_texto(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def _limpio(valor: str | None, maximo: int, campo: str, obligatorio: bool = True) -> str | None:
    valor = (valor or "").strip()
    if not valor:
        if obligatorio:
            raise ErrorNegocio(f"Complete el campo «{campo}».")
        return None
    if len(valor) > maximo:
        raise ErrorNegocio(f"«{campo}» admite hasta {maximo} caracteres.")
    return valor


def exigir_anio_activo(db: Session) -> AnioLectivo:
    anio = anio_activo(db)
    if anio is None:
        raise ErrorNegocio("Primero active un año lectivo en Estructura académica.")
    return anio


# ===================== Usuarios y roles =====================

def sugerir_login(db: Session, nombres: str, apellidos: str) -> str:
    """nombre.apellido sin tildes; agrega un número si ya existe."""
    def simple(texto: str) -> str:
        sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z]", "", sin_tildes.lower().split(" ")[0]) if sin_tildes.strip() else ""
    base = f"{simple(nombres)}.{simple(apellidos)}".strip(".") or "usuario"
    candidato, n = base, 1
    while db.scalar(select(Usuario.id).where(Usuario.login == candidato)) is not None:
        n += 1
        candidato = f"{base}{n}"
    return candidato


def crear_usuario(db: Session, settings: Settings, autor: Autor, *, nombres: str, apellidos: str,
                  email: str | None, roles: list[tuple[str, int | None]], login: str | None = None,
                  persona_id: int | None = None) -> tuple[Usuario, str]:
    """Crea la cuenta sin contraseña y devuelve el enlace de activación de un solo uso."""
    if not roles:
        raise ErrorNegocio("Asigne al menos un rol.")
    for rol, _ in roles:
        if rol not in ROLES:
            raise ErrorNegocio("Rol no válido.")
    email = (_limpio(email, 160, "Correo", obligatorio=False) or "").lower() or None
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ErrorNegocio("Revise el formato del correo.")
    if persona_id is None:
        persona = Persona(nombres=_limpio(nombres, 80, "Nombres"), apellidos=_limpio(apellidos, 80, "Apellidos"))
        db.add(persona)
        flush(db)
        persona_id = persona.id
    login = (login or "").strip().lower() or sugerir_login(db, nombres, apellidos)
    if not re.fullmatch(r"[a-z0-9._-]{3,60}", login):
        raise ErrorNegocio("El usuario solo admite letras sin tildes, números, punto, guion y guion bajo.")
    usuario = Usuario(persona_id=persona_id, login=login, email=email)
    db.add(usuario)
    flush(db)
    for rol, area_id in dict.fromkeys(roles):
        db.add(UsuarioRol(usuario_id=usuario.id, rol=rol, area_id=area_id if rol == "directivo" else None))
    token = emitir_token_cuenta(db, settings, usuario, "activacion")
    _bitacora(db, autor, "crear", "usuario", usuario.id,
              {"login": login, "roles": [r for r, _ in roles]})
    commit(db)
    return usuario, token


def asignar_rol(db: Session, autor: Autor, usuario_id: int, rol: str, area_id: int | None) -> None:
    usuario = db.get(Usuario, usuario_id)
    if usuario is None:
        raise NoEncontrado("Usuario no encontrado.")
    if rol not in ROLES:
        raise ErrorNegocio("Rol no válido.")
    area_id = area_id if rol == "directivo" else None
    if any(r.rol == rol and r.area_id == area_id for r in usuario.roles):
        raise Conflicto("El usuario ya tiene ese rol.")
    db.add(UsuarioRol(usuario_id=usuario.id, rol=rol, area_id=area_id))
    _bitacora(db, autor, "asignar_rol", "usuario", usuario.id, {"rol": rol, "area_id": area_id})
    commit(db)


def quitar_rol(db: Session, autor: Autor, usuario_rol_id: int) -> None:
    registro = db.get(UsuarioRol, usuario_rol_id)
    if registro is None:
        raise NoEncontrado("Rol no encontrado.")
    if registro.usuario_id == autor.usuario_id and registro.rol == "administrador":
        raise ErrorNegocio("No puede quitarse a sí mismo el rol de administrador.")
    _bitacora(db, autor, "quitar_rol", "usuario", registro.usuario_id, {"rol": registro.rol})
    db.delete(registro)
    commit(db)


def cambiar_activo_usuario(db: Session, autor: Autor, usuario_id: int, activo: bool) -> Usuario:
    usuario = db.get(Usuario, usuario_id)
    if usuario is None:
        raise NoEncontrado("Usuario no encontrado.")
    if usuario.id == autor.usuario_id and not activo:
        raise ErrorNegocio("No puede desactivar su propia cuenta.")
    usuario.activo = activo
    if not activo:
        sesiones.revocar_todas(db, usuario.id)
    _bitacora(db, autor, "activar" if activo else "desactivar", "usuario", usuario.id)
    commit(db)
    return usuario


def cerrar_sesiones_usuario(db: Session, autor: Autor, usuario_id: int) -> int:
    cerradas = sesiones.revocar_todas(db, usuario_id)
    _bitacora(db, autor, "cerrar_sesiones", "usuario", usuario_id, {"cerradas": cerradas})
    commit(db)
    return cerradas


def generar_enlace_cuenta(db: Session, settings: Settings, autor: Autor, usuario_id: int) -> tuple[Usuario, str, str]:
    """Activación si nunca activó la cuenta; restablecimiento si ya la usaba."""
    usuario = db.get(Usuario, usuario_id)
    if usuario is None:
        raise NoEncontrado("Usuario no encontrado.")
    tipo = "activacion" if usuario.password_hash is None else "recuperacion"
    token = emitir_token_cuenta(db, settings, usuario, tipo)
    _bitacora(db, autor, f"emitir_enlace_{tipo}", "usuario", usuario.id)
    commit(db)
    return usuario, tipo, token


def vincular_representante(db: Session, autor: Autor, usuario_id: int, alumno_id: int, parentesco: str,
                           representante_legal: bool = True) -> RepresentanteAlumno:
    usuario = db.get(Usuario, usuario_id)
    if usuario is None or not any(r.rol == "representante" for r in usuario.roles):
        raise ErrorNegocio("Primero asigne el rol de representante a este usuario.")
    if db.get(Alumno, alumno_id) is None:
        raise NoEncontrado("Alumno no encontrado.")
    vinculo = RepresentanteAlumno(
        usuario_id=usuario_id, alumno_id=alumno_id, parentesco=_limpio(parentesco, 40, "Parentesco"),
        representante_legal=representante_legal, validado_por=autor.usuario_id, validado_en=ahora(),
    )
    db.add(vinculo)
    flush(db)
    _bitacora(db, autor, "vincular_representante", "alumno", alumno_id,
              {"usuario_id": usuario_id, "parentesco": vinculo.parentesco})
    commit(db)
    return vinculo


def desvincular_representante(db: Session, autor: Autor, vinculo_id: int) -> None:
    vinculo = db.get(RepresentanteAlumno, vinculo_id)
    if vinculo is None:
        raise NoEncontrado("Vínculo no encontrado.")
    _bitacora(db, autor, "desvincular_representante", "alumno", vinculo.alumno_id, {"usuario_id": vinculo.usuario_id})
    db.delete(vinculo)
    commit(db)


# ===================== Estructura académica =====================

def crear_anio(db: Session, autor: Autor, nombre: str, inicio: date, fin: date) -> AnioLectivo:
    if fin <= inicio:
        raise ErrorNegocio("La fecha de fin debe ser posterior a la de inicio.")
    anio = AnioLectivo(nombre=_limpio(nombre, 20, "Nombre"), inicio=inicio, fin=fin, activo=False)
    db.add(anio)
    flush(db)
    _bitacora(db, autor, "crear", "anio_lectivo", anio.id, {"nombre": anio.nombre})
    commit(db)
    return anio


def activar_anio(db: Session, autor: Autor, anio_id: int) -> AnioLectivo:
    anio = db.get(AnioLectivo, anio_id)
    if anio is None:
        raise NoEncontrado("Año lectivo no encontrado.")
    db.execute(update(AnioLectivo).where(AnioLectivo.id != anio.id).values(activo=False))
    db.flush()
    anio.activo = True
    _bitacora(db, autor, "activar", "anio_lectivo", anio.id, {"nombre": anio.nombre})
    commit(db)
    return anio


def crear_periodo(db: Session, autor: Autor, nombre: str, inicio: date, fin: date) -> PeriodoCalificacion:
    anio = exigir_anio_activo(db)
    if fin <= inicio:
        raise ErrorNegocio("La fecha de fin debe ser posterior a la de inicio.")
    if inicio < anio.inicio or fin > anio.fin:
        raise ErrorNegocio("El periodo debe estar dentro de las fechas del año lectivo.")
    periodo = PeriodoCalificacion(anio_lectivo_id=anio.id, nombre=_limpio(nombre, 40, "Nombre"), inicio=inicio, fin=fin)
    db.add(periodo)
    flush(db)
    _bitacora(db, autor, "crear", "periodo", periodo.id, {"nombre": periodo.nombre})
    commit(db)
    return periodo


def crear_area(db: Session, autor: Autor, nombre: str) -> Area:
    area = Area(nombre=_limpio(nombre, 80, "Nombre"))
    db.add(area)
    flush(db)
    _bitacora(db, autor, "crear", "area", area.id, {"nombre": area.nombre})
    commit(db)
    return area


def crear_materia(db: Session, autor: Autor, nombre: str, area_id: int) -> Materia:
    if db.get(Area, area_id) is None:
        raise NoEncontrado("Área no encontrada.")
    materia = Materia(nombre=_limpio(nombre, 80, "Nombre"), area_id=area_id)
    db.add(materia)
    flush(db)
    _bitacora(db, autor, "crear", "materia", materia.id, {"nombre": materia.nombre})
    commit(db)
    return materia


def crear_paralelo(db: Session, autor: Autor, nombre: str) -> Paralelo:
    paralelo = Paralelo(nombre=_limpio(nombre, 4, "Paralelo").upper())
    db.add(paralelo)
    flush(db)
    _bitacora(db, autor, "crear", "paralelo", paralelo.id, {"nombre": paralelo.nombre})
    commit(db)
    return paralelo


def _exigir_docente(db: Session, usuario_id: int | None) -> int | None:
    if usuario_id is None:
        return None
    usuario = db.get(Usuario, usuario_id)
    if usuario is None or not any(r.rol == "docente" for r in usuario.roles):
        raise ErrorNegocio("La persona elegida no tiene rol docente.")
    return usuario.id


def crear_salon(db: Session, autor: Autor, grado_id: int, paralelo_id: int, tutor_id: int | None = None) -> Salon:
    anio = exigir_anio_activo(db)
    grado, paralelo = db.get(Grado, grado_id), db.get(Paralelo, paralelo_id)
    if grado is None or paralelo is None:
        raise NoEncontrado("Grado o paralelo no encontrado.")
    salon = Salon(anio_lectivo_id=anio.id, grado_id=grado.id, paralelo_id=paralelo.id,
                  nombre=f"{grado.nombre} {paralelo.nombre}", tutor_id=_exigir_docente(db, tutor_id))
    db.add(salon)
    flush(db)
    _bitacora(db, autor, "crear", "salon", salon.id, {"nombre": salon.nombre, "anio": anio.nombre})
    commit(db)
    return salon


def asignar_tutor(db: Session, autor: Autor, salon_id: int, tutor_id: int | None) -> Salon:
    salon = db.get(Salon, salon_id)
    if salon is None:
        raise NoEncontrado("Salón no encontrado.")
    salon.tutor_id = _exigir_docente(db, tutor_id)
    _bitacora(db, autor, "asignar_tutor", "salon", salon.id, {"tutor_id": tutor_id})
    commit(db)
    return salon


def asignar_docente(db: Session, autor: Autor, salon_id: int, materia_id: int, docente_id: int | None) -> SalonMateriaDocente:
    if db.get(Salon, salon_id) is None or db.get(Materia, materia_id) is None:
        raise NoEncontrado("Salón o materia no encontrado.")
    asignacion = db.scalar(select(SalonMateriaDocente).where(
        SalonMateriaDocente.salon_id == salon_id, SalonMateriaDocente.materia_id == materia_id))
    if asignacion is None:
        asignacion = SalonMateriaDocente(salon_id=salon_id, materia_id=materia_id)
        db.add(asignacion)
    asignacion.docente_id = _exigir_docente(db, docente_id)
    flush(db)
    _bitacora(db, autor, "asignar_docente", "salon", salon_id, {"materia_id": materia_id, "docente_id": docente_id})
    commit(db)
    return asignacion


# ===================== Alumnos y matrículas =====================

def _matricular(db: Session, alumno: Alumno, salon: Salon) -> Matricula:
    anio = exigir_anio_activo(db)
    if salon.anio_lectivo_id != anio.id:
        raise ErrorNegocio("Solo se puede matricular en salones del año lectivo activo.")
    matricula = db.scalar(select(Matricula).where(Matricula.alumno_id == alumno.id, Matricula.anio_lectivo_id == anio.id))
    if matricula is None:
        matricula = Matricula(alumno_id=alumno.id, salon_id=salon.id, anio_lectivo_id=anio.id)
        db.add(matricula)
    else:
        matricula.salon_id, matricula.estado = salon.id, "activa"
    alumno.salon_id = salon.id  # salón vigente, usado por portería y autorizaciones
    alumno.activo = True
    return matricula


def crear_alumno(db: Session, autor: Autor, *, codigo_interno: str, nombres: str, apellidos: str,
                 salon_id: int) -> Alumno:
    salon = db.get(Salon, salon_id)
    if salon is None:
        raise NoEncontrado("Salón no encontrado.")
    persona = Persona(nombres=_limpio(nombres, 80, "Nombres"), apellidos=_limpio(apellidos, 80, "Apellidos"))
    db.add(persona)
    flush(db)
    alumno = Alumno(persona_id=persona.id, codigo_interno=_limpio(codigo_interno, 20, "Código"))
    db.add(alumno)
    flush(db)
    _matricular(db, alumno, salon)
    flush(db)
    _bitacora(db, autor, "crear", "alumno", alumno.id, {"codigo_interno": alumno.codigo_interno, "salon": salon.nombre})
    commit(db)
    db.refresh(alumno)
    return alumno


def matricular(db: Session, autor: Autor, alumno_id: int, salon_id: int) -> Matricula:
    alumno, salon = db.get(Alumno, alumno_id), db.get(Salon, salon_id)
    if alumno is None or salon is None:
        raise NoEncontrado("Alumno o salón no encontrado.")
    matricula = _matricular(db, alumno, salon)
    flush(db)
    _bitacora(db, autor, "matricular", "alumno", alumno.id, {"salon": salon.nombre})
    commit(db)
    return matricula


def cambiar_activo_alumno(db: Session, autor: Autor, alumno_id: int, activo: bool) -> Alumno:
    """Desactivar retira la matrícula del año activo; activar la reabre."""
    alumno = db.get(Alumno, alumno_id)
    if alumno is None:
        raise NoEncontrado("Alumno no encontrado.")
    alumno.activo = activo
    anio = anio_activo(db)
    if anio is not None:
        db.execute(update(Matricula).where(Matricula.alumno_id == alumno.id, Matricula.anio_lectivo_id == anio.id)
                   .values(estado="activa" if activo else "retirada"))
    _bitacora(db, autor, "actualizar", "alumno", alumno.id, {"activo": activo})
    commit(db)
    db.refresh(alumno)
    return alumno


# ---------- Importación desde CSV ----------

COLUMNAS_CSV = ("codigo_interno", "nombres", "apellidos", "salon")
MAX_BYTES_CSV = 1_000_000
MAX_FILAS_CSV = 2000


@dataclass
class FilaImportacion:
    linea: int
    codigo_interno: str
    nombres: str
    apellidos: str
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
        return contenido.decode("cp1252")  # Excel en Windows


def analizar_csv(db: Session, texto: str) -> list[FilaImportacion]:
    if not texto.strip():
        raise ErrorNegocio("El archivo está vacío.")
    anio = exigir_anio_activo(db)
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

    salones = {s.nombre.strip().lower(): s.id
               for s in db.scalars(select(Salon).where(Salon.anio_lectivo_id == anio.id))}
    existentes = set(db.scalars(select(Alumno.codigo_interno)))
    vistos: set[str] = set()
    filas: list[FilaImportacion] = []
    for linea, registro in enumerate(lector, start=2):
        if len(filas) >= MAX_FILAS_CSV:
            raise ErrorNegocio(f"El archivo tiene más de {MAX_FILAS_CSV} filas.")
        valores = {c: (registro.get(c) or "").strip() for c in COLUMNAS_CSV}
        if not any(valores.values()):
            continue
        salon_id = salones.get(valores["salon"].lower())
        fila = FilaImportacion(linea, valores["codigo_interno"], valores["nombres"], valores["apellidos"],
                               valores["salon"], salon_id, "valida")
        if not all(valores.values()):
            fila.estado, fila.mensaje = "error", "Faltan datos obligatorios."
        elif len(fila.codigo_interno) > 20 or len(fila.nombres) > 80 or len(fila.apellidos) > 80:
            fila.estado, fila.mensaje = "error", "Algún dato es demasiado largo."
        elif salon_id is None:
            fila.estado, fila.mensaje = "error", f"El salón «{fila.salon}» no existe en el año activo."
        elif fila.codigo_interno in vistos:
            fila.estado, fila.mensaje = "duplicada", "Código repetido dentro del archivo."
        elif fila.codigo_interno in existentes:
            fila.estado, fila.mensaje = "duplicada", "Ya existe un alumno con este código."
        if fila.codigo_interno:
            vistos.add(fila.codigo_interno)
        filas.append(fila)
    return filas


def importar_alumnos(db: Session, autor: Autor, texto: str) -> tuple[int, int]:
    """Crea y matricula solo las filas válidas, en una sola transacción. Devuelve (creados, omitidos)."""
    filas = analizar_csv(db, texto)
    validas = [f for f in filas if f.estado == "valida"]
    salones = {s.id: s for s in db.scalars(select(Salon).where(Salon.id.in_({f.salon_id for f in validas})))}
    for fila in validas:
        persona = Persona(nombres=fila.nombres, apellidos=fila.apellidos)
        db.add(persona)
        db.flush()
        alumno = Alumno(persona_id=persona.id, codigo_interno=fila.codigo_interno)
        db.add(alumno)
        db.flush()
        _matricular(db, alumno, salones[fila.salon_id])
    flush(db)
    omitidos = len(filas) - len(validas)
    _bitacora(db, autor, "importar", "alumno", None, {"creados": len(validas), "omitidos": omitidos})
    commit(db)
    return len(validas), omitidos


# ===================== Autorizaciones y recogida =====================

def crear_familia(db: Session, autor: Autor, *, nombre_familia: str, telefono: str | None, email: str | None,
                  consentimiento_mensajes: bool) -> Familia:
    familia = Familia(
        nombre_familia=_limpio(nombre_familia, 120, "Nombre de la familia"),
        telefono=_limpio(telefono, 20, "Teléfono", obligatorio=False),
        email=_limpio(email, 160, "Correo", obligatorio=False),
        consentimiento_mensajes=consentimiento_mensajes,
    )
    db.add(familia)
    flush(db)
    _bitacora(db, autor, "crear", "familia", familia.id,
              {"nombre_familia": familia.nombre_familia, "consentimiento_mensajes": consentimiento_mensajes})
    commit(db)
    return familia


def vincular_alumno(db: Session, autor: Autor, familia_id: int, alumno_id: int) -> None:
    if db.get(Familia, familia_id) is None or db.get(Alumno, alumno_id) is None:
        raise NoEncontrado("Familia o alumno no encontrado.")
    if db.get(FamiliaAlumno, (familia_id, alumno_id)) is None:
        db.add(FamiliaAlumno(familia_id=familia_id, alumno_id=alumno_id))
    _bitacora(db, autor, "vincular_alumno", "familia", familia_id, {"alumno_id": alumno_id})
    commit(db)


def generar_enlace(db: Session, settings: Settings, autor: Autor, familia_id: int) -> tuple[str, datetime]:
    """Devuelve el token en claro (solo esta vez) y su vencimiento. Invalida los enlaces anteriores."""
    if db.get(Familia, familia_id) is None:
        raise NoEncontrado("Familia no encontrada.")
    momento = ahora()
    db.execute(update(TokenAcceso).where(TokenAcceso.familia_id == familia_id, TokenAcceso.expira_en > momento)
               .values(expira_en=momento))
    token = security.generate_token()
    expira = momento + timedelta(days=settings.family_link_ttl_days)
    db.add(TokenAcceso(familia_id=familia_id, token_hash=security.hash_secret(token, settings.token_hmac_key),
                       expira_en=expira))
    _bitacora(db, autor, "generar_enlace", "familia", familia_id, {"expira_en": expira.isoformat()})
    commit(db)
    return token, expira


def agregar_adulto(db: Session, autor: Autor, *, alumno_id: int, nombre: str, parentesco: str,
                   telefono: str | None, familia_id: int | None) -> AdultoAutorizado:
    if db.get(Alumno, alumno_id) is None:
        raise NoEncontrado("Alumno no encontrado.")
    if familia_id is not None and db.get(Familia, familia_id) is None:
        raise NoEncontrado("Familia no encontrada.")
    adulto = AdultoAutorizado(alumno_id=alumno_id, familia_id=familia_id, nombre=_limpio(nombre, 120, "Nombre"),
                              parentesco=_limpio(parentesco, 40, "Parentesco"),
                              telefono=_limpio(telefono, 20, "Teléfono", obligatorio=False), estado="pendiente")
    db.add(adulto)
    flush(db)
    _bitacora(db, autor, "crear", "adulto_autorizado", adulto.id, {"alumno_id": alumno_id, "parentesco": adulto.parentesco})
    commit(db)
    return adulto


def decidir_adulto(db: Session, autor: Autor, adulto_id: int, estado: str) -> AdultoAutorizado:
    if estado not in ("validado", "rechazado"):
        raise ErrorNegocio("Estado no válido.")
    adulto = db.get(AdultoAutorizado, adulto_id)
    if adulto is None:
        raise NoEncontrado("Adulto no encontrado.")
    anterior = adulto.estado
    adulto.estado = estado
    adulto.validado_por = autor.usuario_id
    _bitacora(db, autor, "validar" if estado == "validado" else "rechazar", "adulto_autorizado", adulto.id,
              {"de": anterior, "a": estado})
    commit(db)
    return adulto


def crear_campana(db: Session, autor: Autor, *, titulo: str, descripcion: str | None, tipo: str,
                  texto_legal: str, fecha_limite: datetime, salon_ids: list[int]) -> tuple[Campana, list[int]]:
    salon_ids = sorted(set(salon_ids))
    if not salon_ids:
        raise ErrorNegocio("Elija al menos un salón.")
    encontrados = db.scalars(select(Salon.id).where(Salon.id.in_(salon_ids))).all()
    if len(encontrados) != len(salon_ids):
        raise NoEncontrado("Uno de los salones no existe.")
    if not texto_legal.strip():
        raise ErrorNegocio("El texto de la autorización es obligatorio.")
    campana = Campana(titulo=_limpio(titulo, 160, "Título"), descripcion=(descripcion or "").strip() or None,
                      tipo=tipo, texto_legal=texto_legal, texto_hash=hash_texto(texto_legal),
                      fecha_limite=a_utc(fecha_limite), creado_por=autor.usuario_id)
    db.add(campana)
    flush(db)
    for salon_id in salon_ids:
        db.add(CampanaSalon(campana_id=campana.id, salon_id=salon_id))
    _bitacora(db, autor, "crear", "campana", campana.id,
              {"titulo": campana.titulo, "tipo": tipo, "salon_ids": salon_ids, "texto_hash": campana.texto_hash})
    commit(db)
    return campana, salon_ids


def progreso_campana(db: Session, campana_id: int) -> list[dict]:
    """Avance por salón: alumnos activos, aceptadas, rechazadas y pendientes."""
    salones = db.scalars(select(Salon).join(CampanaSalon, CampanaSalon.salon_id == Salon.id)
                         .where(CampanaSalon.campana_id == campana_id).order_by(Salon.nombre)).all()
    filas = []
    for salon in salones:
        total = db.scalar(select(func.count()).select_from(Alumno)
                          .where(Alumno.salon_id == salon.id, Alumno.activo.is_(True)))
        conteo = dict(db.execute(
            select(RespuestaAutorizacion.decision, func.count())
            .join(Alumno, Alumno.id == RespuestaAutorizacion.alumno_id)
            .where(RespuestaAutorizacion.campana_id == campana_id, Alumno.salon_id == salon.id,
                   Alumno.activo.is_(True))
            .group_by(RespuestaAutorizacion.decision)
        ).all())
        aceptadas, rechazadas = conteo.get("aceptada", 0), conteo.get("rechazada", 0)
        filas.append({"salon_id": salon.id, "salon": salon.nombre, "total": total, "aceptadas": aceptadas,
                      "rechazadas": rechazadas, "pendientes": max(total - aceptadas - rechazadas, 0)})
    return filas


def totalizar(filas: list[dict]) -> dict:
    claves = ("total", "aceptadas", "rechazadas", "pendientes")
    totales = {c: sum(f[c] for f in filas) for c in claves}
    respondidas = totales["aceptadas"] + totales["rechazadas"]
    totales["avance"] = round(100 * respondidas / totales["total"]) if totales["total"] else 0
    return totales
