import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import (
    Alumno,
    Base,
    CodigoRecogida,
    Familia,
    Recogida,
    RespuestaAutorizacion,
    Salon,
    UsuarioStaff,
    Campana,
)

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


def _base(session):
    salon = Salon(nombre="1A", grado="1ro")
    session.add(salon)
    session.flush()
    alumno = Alumno(codigo_interno="A-001", nombre="Alumno Ficticio 1", salon_id=salon.id)
    familia = Familia(nombre_familia="Familia Ficticia")
    staff = UsuarioStaff(nombre="Staff", email="s@ejemplo.test", rol="coordinadora", password_hash="x")
    session.add_all([alumno, familia, staff])
    session.flush()
    return salon, alumno, familia, staff


def test_schema_creates_all_tables(session):
    names = set(Base.metadata.tables)
    esperadas = {
        "usuarios_staff", "salones", "alumnos", "familias", "familia_alumno",
        "adultos_autorizados", "campanas_autorizacion", "campana_salones",
        "respuestas_autorizacion", "tokens_acceso", "codigos_recogida",
        "recogidas", "incidentes", "bitacora_auditoria",
    }
    assert esperadas <= names


def test_rol_invalido_es_rechazado(session):
    session.add(UsuarioStaff(nombre="X", email="x@ejemplo.test", rol="directora", password_hash="x"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_una_respuesta_por_alumno_y_campana(session):
    _, alumno, familia, staff = _base(session)
    campana = Campana(
        titulo="Salida", tipo="salida", texto_legal="Texto", texto_hash="h",
        fecha_limite=NOW + timedelta(days=5), creado_por=staff.id,
    )
    session.add(campana)
    session.flush()
    datos = dict(
        campana_id=campana.id, alumno_id=alumno.id, familia_id=familia.id,
        firmante="Firmante", parentesco="madre", decision="aceptada",
        canal="enlace", texto_hash="h",
    )
    session.add(RespuestaAutorizacion(**datos))
    session.flush()
    session.add(RespuestaAutorizacion(**datos))
    with pytest.raises(IntegrityError):
        session.flush()


def test_recogida_requiere_adulto_o_marca_no_autorizado(session):
    _, alumno, _, staff = _base(session)
    session.add(Recogida(
        alumno_id=alumno.id, adulto_id=None, no_autorizado=False,
        registrada_por=staff.id, ocurrida_en=NOW,
    ))
    with pytest.raises(IntegrityError):
        session.flush()


def test_recogida_no_autorizada_es_valida(session):
    _, alumno, _, staff = _base(session)
    session.add(Recogida(
        alumno_id=alumno.id, adulto_id=None, no_autorizado=True,
        registrada_por=staff.id, ocurrida_en=NOW,
    ))
    session.flush()
    assert session.scalar(select(Recogida.id)) is not None


def test_sync_id_duplicado_se_rechaza(session):
    _, alumno, _, staff = _base(session)
    datos = dict(
        alumno_id=alumno.id, no_autorizado=True, registrada_por=staff.id,
        ocurrida_en=NOW, sync_id="evento-123",
    )
    session.add(Recogida(**datos))
    session.flush()
    session.add(Recogida(**datos))
    with pytest.raises(IntegrityError):
        session.flush()


def test_solo_un_codigo_activo_por_adulto_y_alumno(session):
    from app.models import AdultoAutorizado

    _, alumno, familia, staff = _base(session)
    adulto = AdultoAutorizado(alumno_id=alumno.id, familia_id=familia.id, nombre="Adulto", parentesco="tía", estado="validado")
    session.add(adulto)
    session.flush()
    session.add(CodigoRecogida(adulto_id=adulto.id, alumno_id=alumno.id, codigo_hash="a", expira_en=NOW))
    session.flush()
    session.add(CodigoRecogida(adulto_id=adulto.id, alumno_id=alumno.id, codigo_hash="b", expira_en=NOW))
    with pytest.raises(IntegrityError):
        session.flush()


def test_codigo_usado_no_bloquea_uno_nuevo(session):
    from app.models import AdultoAutorizado

    _, alumno, familia, _ = _base(session)
    adulto = AdultoAutorizado(alumno_id=alumno.id, familia_id=familia.id, nombre="Adulto", parentesco="tía", estado="validado")
    session.add(adulto)
    session.flush()
    session.add(CodigoRecogida(adulto_id=adulto.id, alumno_id=alumno.id, codigo_hash="a", expira_en=NOW, usado_en=NOW))
    session.flush()
    session.add(CodigoRecogida(adulto_id=adulto.id, alumno_id=alumno.id, codigo_hash="b", expira_en=NOW))
    session.flush()  # no debe fallar


def test_bitacora_es_inmutable_en_postgres():
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Definir TEST_POSTGRES_URL para probar el trigger de bitácora")

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO bitacora_auditoria (accion, entidad) VALUES ('crear', 'alumno')"
            ))
        with pytest.raises(Exception, match="inmutable"):
            with engine.begin() as conn:
                conn.execute(text("UPDATE bitacora_auditoria SET accion = 'x'"))
        with pytest.raises(Exception, match="inmutable"):
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM bitacora_auditoria"))
    finally:
        command.downgrade(cfg, "base")
        engine.dispose()
