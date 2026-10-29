"""Modelo de datos relacional (SQLAlchemy 2).

Convenciones:
- Las fechas con hora se guardan en UTC; la zona de Ecuador solo se aplica al mostrarlas.
- Los secretos nunca se guardan en claro: contraseñas con argon2, tokens y códigos como HMAC,
  documentos y secretos TOTP cifrados con AES-256-GCM (clave fuera de la base de datos).
- `personas` guarda los datos básicos de cualquier individuo. `usuarios` existe solo si esa
  persona tiene acceso al sistema; un alumno puede existir sin cuenta.
"""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, column_property, mapped_column, relationship

ROLES = (
    "administrador", "coordinacion", "directivo", "docente", "consejeria",
    "auditor", "porteria", "representante", "estudiante",
)
TIPOS_CAMPANA = ("salida", "vacuna", "evento", "otro")
ESTADOS_ADULTO = ("pendiente", "validado", "rechazado")
DECISIONES = ("aceptada", "rechazada")
ESTADOS_SESION = ("pendiente_totp", "enrolar_totp", "completa")
ESTADOS_MATRICULA = ("activa", "retirada", "finalizada")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


class Base(DeclarativeBase):
    pass


def _creado_en():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ===================== Personas, usuarios y roles =====================

class Persona(Base):
    __tablename__ = "personas"

    id: Mapped[int] = mapped_column(primary_key=True)
    nombres: Mapped[str] = mapped_column(String(80))
    apellidos: Mapped[str] = mapped_column(String(80))
    documento_cifrado: Mapped[str | None] = mapped_column(Text, nullable=True)
    documento_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    creada_en: Mapped[datetime] = _creado_en()

    @property
    def nombre_completo(self) -> str:
        return f"{self.nombres} {self.apellidos}".strip()


class Area(Base):
    """Área académica (Lengua, Matemática...). Define el alcance de los directivos de área."""

    __tablename__ = "areas"

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[str] = mapped_column(String(80), unique=True)


class Usuario(Base):
    __tablename__ = "usuarios"

    id: Mapped[int] = mapped_column(primary_key=True)
    persona_id: Mapped[int] = mapped_column(ForeignKey("personas.id"), unique=True)
    login: Mapped[str] = mapped_column(String(60), unique=True)
    email: Mapped[str | None] = mapped_column(String(160), unique=True, nullable=True)
    # Nulo hasta que la persona active su cuenta con el enlace de un solo uso
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    totp_secret_cifrado: Mapped[str | None] = mapped_column(Text, nullable=True)
    totp_pendiente_cifrado: Mapped[str | None] = mapped_column(Text, nullable=True)
    activo: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    intentos_fallidos: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    bloqueado_hasta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    creado_en: Mapped[datetime] = _creado_en()

    persona: Mapped[Persona] = relationship(lazy="joined")
    roles: Mapped[list["UsuarioRol"]] = relationship(back_populates="usuario", lazy="selectin",
                                                      cascade="all, delete-orphan")

    @property
    def nombre(self) -> str:
        return self.persona.nombre_completo


class UsuarioRol(Base):
    """Rol con ámbito: un directivo puede estar limitado a un área; nulo significa toda la institución."""

    __tablename__ = "usuario_roles"
    __table_args__ = (
        CheckConstraint(_in_list("rol", ROLES), name="ck_usuario_rol"),
        UniqueConstraint("usuario_id", "rol", "area_id", name="uq_usuario_rol_area"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    rol: Mapped[str] = mapped_column(String(20))
    area_id: Mapped[int | None] = mapped_column(ForeignKey("areas.id"), nullable=True)
    vigente_hasta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    creado_en: Mapped[datetime] = _creado_en()

    usuario: Mapped[Usuario] = relationship(back_populates="roles")
    area: Mapped[Area | None] = relationship(lazy="joined")


class DispositivoUsuario(Base):
    __tablename__ = "dispositivos_usuario"
    __table_args__ = (UniqueConstraint("usuario_id", "huella_hash", name="uq_dispositivo_usuario"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    # HMAC del identificador aleatorio guardado en una cookie del navegador
    huella_hash: Mapped[str] = mapped_column(String(64))
    descripcion: Mapped[str] = mapped_column(String(120))
    confiable: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    primera_vez: Mapped[datetime] = _creado_en()
    ultima_vez: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Sesion(Base):
    """Sesión guardada en el servidor: permite listar dispositivos y cerrar sesiones a distancia."""

    __tablename__ = "sesiones"
    __table_args__ = (CheckConstraint(_in_list("estado", ESTADOS_SESION), name="ck_sesion_estado"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    dispositivo_id: Mapped[int | None] = mapped_column(ForeignKey("dispositivos_usuario.id"), nullable=True)
    estado: Mapped[str] = mapped_column(String(16))
    rol_activo: Mapped[str | None] = mapped_column(String(20), nullable=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    # JSON con avisos pendientes de mostrar y accesos temporales justificados con motivo
    datos: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    creada_en: Mapped[datetime] = _creado_en()
    ultima_actividad: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expira_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revocada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    usuario: Mapped[Usuario] = relationship(lazy="joined")


class AlertaInicioSesion(Base):
    __tablename__ = "alertas_inicio_sesion"
    __table_args__ = (
        CheckConstraint("estado IN ('pendiente', 'confirmada', 'bloqueada')", name="ck_alerta_estado"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    sesion_id: Mapped[int | None] = mapped_column(ForeignKey("sesiones.id"), nullable=True)
    motivo: Mapped[str] = mapped_column(String(200))
    estado: Mapped[str] = mapped_column(String(12), default="pendiente", server_default=text("'pendiente'"))
    creada_en: Mapped[datetime] = _creado_en()
    resuelta_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TokenCuenta(Base):
    """Enlaces de un solo uso para activar la cuenta o restablecer la contraseña."""

    __tablename__ = "tokens_cuenta"
    __table_args__ = (CheckConstraint("tipo IN ('activacion', 'recuperacion')", name="ck_token_cuenta_tipo"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    tipo: Mapped[str] = mapped_column(String(12))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expira_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    usado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    creado_en: Mapped[datetime] = _creado_en()


class EnvioSaliente(Base):
    """Bandeja de salida (correo, WhatsApp, SMS). Un worker la procesará; en la demo se muestra en pantalla."""

    __tablename__ = "envios"
    __table_args__ = (
        CheckConstraint("canal IN ('correo', 'whatsapp', 'sms')", name="ck_envio_canal"),
        CheckConstraint("estado IN ('pendiente', 'enviado', 'error')", name="ck_envio_estado"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    canal: Mapped[str] = mapped_column(String(10))
    destino: Mapped[str] = mapped_column(String(160))
    asunto: Mapped[str] = mapped_column(String(160))
    cuerpo: Mapped[str] = mapped_column(Text)
    estado: Mapped[str] = mapped_column(String(10), default="pendiente", server_default=text("'pendiente'"))
    creado_en: Mapped[datetime] = _creado_en()


# ===================== Estructura académica =====================

class AnioLectivo(Base):
    __tablename__ = "anios_lectivos"
    __table_args__ = (
        CheckConstraint("fin > inicio", name="ck_anio_fechas"),
        # Solo un año lectivo activo a la vez
        Index("uq_anio_activo", "activo", unique=True,
              postgresql_where=text("activo"), sqlite_where=text("activo")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[str] = mapped_column(String(20), unique=True)
    inicio: Mapped[date] = mapped_column(Date)
    fin: Mapped[date] = mapped_column(Date)
    activo: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class PeriodoCalificacion(Base):
    __tablename__ = "periodos_calificacion"
    __table_args__ = (
        UniqueConstraint("anio_lectivo_id", "nombre", name="uq_periodo_nombre"),
        CheckConstraint("fin > inicio", name="ck_periodo_fechas"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    anio_lectivo_id: Mapped[int] = mapped_column(ForeignKey("anios_lectivos.id"))
    nombre: Mapped[str] = mapped_column(String(40))
    inicio: Mapped[date] = mapped_column(Date)
    fin: Mapped[date] = mapped_column(Date)
    cerrado: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class Nivel(Base):
    __tablename__ = "niveles"

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[str] = mapped_column(String(60), unique=True)
    orden: Mapped[int] = mapped_column(Integer)


class Grado(Base):
    __tablename__ = "grados"

    id: Mapped[int] = mapped_column(primary_key=True)
    nivel_id: Mapped[int] = mapped_column(ForeignKey("niveles.id"))
    nombre: Mapped[str] = mapped_column(String(20), unique=True)
    orden: Mapped[int] = mapped_column(Integer)

    nivel: Mapped[Nivel] = relationship(lazy="joined")


class Paralelo(Base):
    __tablename__ = "paralelos"

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[str] = mapped_column(String(4), unique=True)


class Materia(Base):
    __tablename__ = "materias"

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[str] = mapped_column(String(80), unique=True)
    area_id: Mapped[int] = mapped_column(ForeignKey("areas.id"))

    area: Mapped[Area] = relationship(lazy="joined")


class Salon(Base):
    """Grado + paralelo + año lectivo. `nombre` ("5.º A") se guarda para mostrar y buscar rápido."""

    __tablename__ = "salones"
    __table_args__ = (
        UniqueConstraint("anio_lectivo_id", "grado_id", "paralelo_id", name="uq_salon_grado_paralelo"),
        UniqueConstraint("anio_lectivo_id", "nombre", name="uq_salon_nombre"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    anio_lectivo_id: Mapped[int] = mapped_column(ForeignKey("anios_lectivos.id"), index=True)
    grado_id: Mapped[int] = mapped_column(ForeignKey("grados.id"))
    paralelo_id: Mapped[int] = mapped_column(ForeignKey("paralelos.id"))
    nombre: Mapped[str] = mapped_column(String(30))
    tutor_id: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id"), nullable=True)

    grado: Mapped[Grado] = relationship(lazy="joined")
    tutor: Mapped[Usuario | None] = relationship(lazy="joined")


class SalonMateriaDocente(Base):
    __tablename__ = "salon_materia_docente"
    __table_args__ = (UniqueConstraint("salon_id", "materia_id", name="uq_salon_materia"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    salon_id: Mapped[int] = mapped_column(ForeignKey("salones.id"), index=True)
    materia_id: Mapped[int] = mapped_column(ForeignKey("materias.id"))
    docente_id: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id"), nullable=True, index=True)

    materia: Mapped[Materia] = relationship(lazy="joined")
    docente: Mapped[Usuario | None] = relationship(lazy="joined")


class Alumno(Base):
    """Expediente del alumno. `salon_id` es el salón vigente: se mantiene al matricular en el año activo
    para que portería y autorizaciones lo consulten sin recorrer el historial de matrículas."""

    __tablename__ = "alumnos"
    __table_args__ = (UniqueConstraint("codigo_interno", name="uq_alumno_codigo"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    persona_id: Mapped[int] = mapped_column(ForeignKey("personas.id"), unique=True)
    codigo_interno: Mapped[str] = mapped_column(String(20))
    salon_id: Mapped[int | None] = mapped_column(ForeignKey("salones.id"), index=True, nullable=True)
    activo: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))

    persona: Mapped[Persona] = relationship(lazy="joined")


# Nombre completo consultable (filtros, orden) sin duplicar el dato en dos tablas
Alumno.nombre = column_property(
    select(Persona.nombres + " " + Persona.apellidos)
    .where(Persona.id == Alumno.persona_id)
    .correlate_except(Persona)
    .scalar_subquery()
)


class Matricula(Base):
    __tablename__ = "matriculas"
    __table_args__ = (
        UniqueConstraint("alumno_id", "anio_lectivo_id", name="uq_matricula_anio"),
        CheckConstraint(_in_list("estado", ESTADOS_MATRICULA), name="ck_matricula_estado"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    alumno_id: Mapped[int] = mapped_column(ForeignKey("alumnos.id"), index=True)
    salon_id: Mapped[int] = mapped_column(ForeignKey("salones.id"), index=True)
    anio_lectivo_id: Mapped[int] = mapped_column(ForeignKey("anios_lectivos.id"))
    estado: Mapped[str] = mapped_column(String(12), default="activa", server_default=text("'activa'"))
    creada_en: Mapped[datetime] = _creado_en()


class RepresentanteAlumno(Base):
    """Vínculo representante–alumno. Lo crea y valida la administración; el padre no puede vincularse solo."""

    __tablename__ = "representantes_alumno"
    __table_args__ = (UniqueConstraint("usuario_id", "alumno_id", name="uq_representante_alumno"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    alumno_id: Mapped[int] = mapped_column(ForeignKey("alumnos.id"), index=True)
    parentesco: Mapped[str] = mapped_column(String(40))
    representante_legal: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    validado_por: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    validado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ===================== Autorizaciones y recogida (módulo 2.11) =====================

class Familia(Base):
    """Grupo de contacto para los enlaces de autorización (sin cuenta con contraseña)."""

    __tablename__ = "familias"

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre_familia: Mapped[str] = mapped_column(String(120))
    telefono: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email: Mapped[str | None] = mapped_column(String(160), nullable=True)
    consentimiento_mensajes: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class FamiliaAlumno(Base):
    __tablename__ = "familia_alumno"

    familia_id: Mapped[int] = mapped_column(ForeignKey("familias.id"), primary_key=True)
    alumno_id: Mapped[int] = mapped_column(ForeignKey("alumnos.id"), primary_key=True)


class AdultoAutorizado(Base):
    __tablename__ = "adultos_autorizados"
    __table_args__ = (
        CheckConstraint(_in_list("estado", ESTADOS_ADULTO), name="ck_adulto_estado"),
        CheckConstraint("vigente_hasta IS NULL OR vigente_hasta > vigente_desde", name="ck_adulto_vigencia"),
        Index("ix_adulto_alumno_estado", "alumno_id", "estado"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    alumno_id: Mapped[int] = mapped_column(ForeignKey("alumnos.id"))
    familia_id: Mapped[int | None] = mapped_column(ForeignKey("familias.id"), nullable=True)
    nombre: Mapped[str] = mapped_column(String(120))
    parentesco: Mapped[str] = mapped_column(String(40))
    telefono: Mapped[str | None] = mapped_column(String(20), nullable=True)
    documento_cifrado: Mapped[str | None] = mapped_column(Text, nullable=True)
    documento_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    estado: Mapped[str] = mapped_column(String(12), default="pendiente", server_default=text("'pendiente'"))
    vigente_desde: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    vigente_hasta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    temporal: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    evidencia: Mapped[str | None] = mapped_column(Text, nullable=True)
    validado_por: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id"), nullable=True)


class Campana(Base):
    __tablename__ = "campanas_autorizacion"
    __table_args__ = (CheckConstraint(_in_list("tipo", TIPOS_CAMPANA), name="ck_campana_tipo"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    titulo: Mapped[str] = mapped_column(String(160))
    descripcion: Mapped[str | None] = mapped_column(Text, nullable=True)
    tipo: Mapped[str] = mapped_column(String(10))
    texto_legal: Mapped[str] = mapped_column(Text)
    # Hash del texto exacto mostrado. Si el texto cambia, la campaña se versiona y no se edita.
    texto_hash: Mapped[str] = mapped_column(String(64))
    fecha_limite: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    creado_por: Mapped[int] = mapped_column(ForeignKey("usuarios.id"))
    creada_en: Mapped[datetime] = _creado_en()


class CampanaSalon(Base):
    __tablename__ = "campana_salones"

    campana_id: Mapped[int] = mapped_column(ForeignKey("campanas_autorizacion.id"), primary_key=True)
    salon_id: Mapped[int] = mapped_column(ForeignKey("salones.id"), primary_key=True)


class RespuestaAutorizacion(Base):
    __tablename__ = "respuestas_autorizacion"
    __table_args__ = (
        UniqueConstraint("campana_id", "alumno_id", name="uq_respuesta_campana_alumno"),
        CheckConstraint(_in_list("decision", DECISIONES), name="ck_respuesta_decision"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    campana_id: Mapped[int] = mapped_column(ForeignKey("campanas_autorizacion.id"))
    alumno_id: Mapped[int] = mapped_column(ForeignKey("alumnos.id"))
    familia_id: Mapped[int] = mapped_column(ForeignKey("familias.id"))
    firmante: Mapped[str] = mapped_column(String(120))
    parentesco: Mapped[str] = mapped_column(String(40))
    decision: Mapped[str] = mapped_column(String(10))
    canal: Mapped[str] = mapped_column(String(20))
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    texto_hash: Mapped[str] = mapped_column(String(64))
    creada_en: Mapped[datetime] = _creado_en()


class TokenAcceso(Base):
    __tablename__ = "tokens_acceso"

    id: Mapped[int] = mapped_column(primary_key=True)
    familia_id: Mapped[int] = mapped_column(ForeignKey("familias.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expira_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    usado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    creado_en: Mapped[datetime] = _creado_en()


class CodigoRecogida(Base):
    __tablename__ = "codigos_recogida"
    __table_args__ = (
        CheckConstraint("intentos >= 0", name="ck_codigo_intentos"),
        Index("uq_codigo_activo", "adulto_id", "alumno_id", unique=True,
              postgresql_where=text("usado_en IS NULL"), sqlite_where=text("usado_en IS NULL")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    adulto_id: Mapped[int] = mapped_column(ForeignKey("adultos_autorizados.id"))
    alumno_id: Mapped[int] = mapped_column(ForeignKey("alumnos.id"))
    codigo_hash: Mapped[str] = mapped_column(String(64))
    expira_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    usado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    intentos: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    creado_en: Mapped[datetime] = _creado_en()


class Recogida(Base):
    __tablename__ = "recogidas"
    __table_args__ = (
        CheckConstraint("adulto_id IS NOT NULL OR no_autorizado", name="ck_recogida_persona"),
        UniqueConstraint("sync_id", name="uq_recogida_sync"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    alumno_id: Mapped[int] = mapped_column(ForeignKey("alumnos.id"), index=True)
    adulto_id: Mapped[int | None] = mapped_column(ForeignKey("adultos_autorizados.id"), nullable=True)
    no_autorizado: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    registrada_por: Mapped[int] = mapped_column(ForeignKey("usuarios.id"))
    verificada_por: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    ocurrida_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sync_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    creada_en: Mapped[datetime] = _creado_en()


class Incidente(Base):
    __tablename__ = "incidentes"

    id: Mapped[int] = mapped_column(primary_key=True)
    recogida_id: Mapped[int | None] = mapped_column(ForeignKey("recogidas.id"), nullable=True)
    descripcion: Mapped[str] = mapped_column(Text)
    personas_involucradas: Mapped[str | None] = mapped_column(Text, nullable=True)
    acciones_tomadas: Mapped[str | None] = mapped_column(Text, nullable=True)
    registrado_por: Mapped[int] = mapped_column(ForeignKey("usuarios.id"))
    creado_en: Mapped[datetime] = _creado_en()


# ===================== Auditoría =====================

class BitacoraAuditoria(Base):
    """Registro de acciones. Inmutable: en PostgreSQL un trigger impide UPDATE y DELETE."""

    __tablename__ = "bitacora_auditoria"

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id"), nullable=True, index=True)
    rol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    accion: Mapped[str] = mapped_column(String(60))
    entidad: Mapped[str] = mapped_column(String(60))
    entidad_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cambio: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Obligatorio cuando el administrador consulta datos de un alumno
    motivo: Mapped[str | None] = mapped_column(String(300), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    creada_en: Mapped[datetime] = _creado_en()
