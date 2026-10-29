"""Modelo de datos relacional (SQLAlchemy 2).

Todas las marcas de tiempo se guardan en UTC. La zona horaria de Ecuador se aplica solo al mostrarlas.
Los documentos de identidad se guardan cifrados (documento_cifrado); el hash permite buscarlos sin descifrar.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ROLES = ("administrador", "coordinadora", "maestra", "porteria")
TIPOS_CAMPANA = ("salida", "vacuna", "evento", "otro")
ESTADOS_ADULTO = ("pendiente", "validado", "rechazado")
DECISIONES = ("aceptada", "rechazada")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


class Base(DeclarativeBase):
    pass


def _creado_en():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class UsuarioStaff(Base):
    __tablename__ = "usuarios_staff"
    __table_args__ = (CheckConstraint(_in_list("rol", ROLES), name="ck_staff_rol"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(160), unique=True)
    rol: Mapped[str] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(String(255))
    # Pendiente de cifrado a nivel de aplicación en una entrega posterior (ver README).
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    activo: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))


class Salon(Base):
    __tablename__ = "salones"
    __table_args__ = (UniqueConstraint("nombre", name="uq_salon_nombre"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[str] = mapped_column(String(60))
    grado: Mapped[str] = mapped_column(String(20))


class Alumno(Base):
    __tablename__ = "alumnos"
    __table_args__ = (UniqueConstraint("codigo_interno", name="uq_alumno_codigo"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    codigo_interno: Mapped[str] = mapped_column(String(20))
    nombre: Mapped[str] = mapped_column(String(120))
    salon_id: Mapped[int] = mapped_column(ForeignKey("salones.id"), index=True)
    activo: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))


class Familia(Base):
    __tablename__ = "familias"

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre_familia: Mapped[str] = mapped_column(String(120))
    telefono: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email: Mapped[str | None] = mapped_column(String(160), nullable=True)
    consentimiento_mensajes: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class FamiliaAlumno(Base):
    """Relación entre familia y alumno (un alumno puede tener varias familias de contacto)."""

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
    validado_por: Mapped[int | None] = mapped_column(ForeignKey("usuarios_staff.id"), nullable=True)


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
    creado_por: Mapped[int] = mapped_column(ForeignKey("usuarios_staff.id"))
    creada_en: Mapped[datetime] = _creado_en()


class CampanaSalon(Base):
    __tablename__ = "campana_salones"

    campana_id: Mapped[int] = mapped_column(ForeignKey("campanas_autorizacion.id"), primary_key=True)
    salon_id: Mapped[int] = mapped_column(ForeignKey("salones.id"), primary_key=True)


class RespuestaAutorizacion(Base):
    __tablename__ = "respuestas_autorizacion"
    __table_args__ = (
        # Una sola respuesta por alumno y campaña
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
    # Solo se guarda el HMAC del token, nunca el token en claro.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expira_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    usado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    creado_en: Mapped[datetime] = _creado_en()


class CodigoRecogida(Base):
    __tablename__ = "codigos_recogida"
    __table_args__ = (
        CheckConstraint("intentos >= 0", name="ck_codigo_intentos"),
        # Como máximo un código vigente (sin usar) por adulto y alumno
        Index(
            "uq_codigo_activo",
            "adulto_id",
            "alumno_id",
            unique=True,
            postgresql_where=text("usado_en IS NULL"),
            sqlite_where=text("usado_en IS NULL"),
        ),
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
        # Debe haber un adulto identificado o marcarse como no autorizado
        CheckConstraint("adulto_id IS NOT NULL OR no_autorizado", name="ck_recogida_persona"),
        # Idempotencia para sincronización offline
        UniqueConstraint("sync_id", name="uq_recogida_sync"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    alumno_id: Mapped[int] = mapped_column(ForeignKey("alumnos.id"), index=True)
    adulto_id: Mapped[int | None] = mapped_column(ForeignKey("adultos_autorizados.id"), nullable=True)
    no_autorizado: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    registrada_por: Mapped[int] = mapped_column(ForeignKey("usuarios_staff.id"))
    verificada_por: Mapped[int | None] = mapped_column(ForeignKey("usuarios_staff.id"), nullable=True)
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
    registrado_por: Mapped[int] = mapped_column(ForeignKey("usuarios_staff.id"))
    creado_en: Mapped[datetime] = _creado_en()


class BitacoraAuditoria(Base):
    """Registro de acciones. Inmutable: en PostgreSQL un trigger impide UPDATE y DELETE."""

    __tablename__ = "bitacora_auditoria"

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int | None] = mapped_column(ForeignKey("usuarios_staff.id"), nullable=True)
    accion: Mapped[str] = mapped_column(String(60))
    entidad: Mapped[str] = mapped_column(String(60))
    entidad_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cambio: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    creada_en: Mapped[datetime] = _creado_en()
