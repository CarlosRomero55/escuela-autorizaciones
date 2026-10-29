import os
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError

from app.models import (
    AdultoAutorizado,
    Alumno,
    AnioLectivo,
    Base,
    Campana,
    CodigoRecogida,
    Familia,
    Matricula,
    Recogida,
    RespuestaAutorizacion,
    UsuarioRol,
)
from tests.conftest import crear_alumno, crear_salon, crear_usuario, estructura

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def test_esquema_incluye_las_tablas_principales():
    esperadas = {
        "personas", "usuarios", "usuario_roles", "sesiones", "dispositivos_usuario", "alertas_inicio_sesion",
        "tokens_cuenta", "anios_lectivos", "periodos_calificacion", "niveles", "grados", "paralelos", "areas",
        "materias", "salones", "salon_materia_docente", "matriculas", "representantes_alumno", "alumnos",
        "familias", "adultos_autorizados", "campanas_autorizacion", "respuestas_autorizacion", "tokens_acceso",
        "codigos_recogida", "recogidas", "incidentes", "bitacora_auditoria", "envios",
    }
    assert esperadas <= set(Base.metadata.tables)


def test_rol_invalido_es_rechazado(session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    with session_factory() as db:
        db.add(UsuarioRol(usuario_id=cuenta.id, rol="superusuario"))
        with pytest.raises(IntegrityError):
            db.flush()


def test_solo_un_anio_lectivo_activo(session_factory):
    estructura(session_factory)
    with session_factory() as db:
        db.add(AnioLectivo(nombre="2027-2028", inicio=date(2027, 5, 1), fin=date(2028, 2, 1), activo=True))
        with pytest.raises(IntegrityError):
            db.flush()


def test_salon_unico_por_grado_paralelo_y_anio(session_factory):
    crear_salon(session_factory, "5.º A")
    with pytest.raises(IntegrityError):
        crear_salon(session_factory, "5.º A")


def test_una_matricula_por_alumno_y_anio(session_factory):
    datos = estructura(session_factory)
    salon_a = crear_salon(session_factory, "5.º A")
    salon_b = crear_salon(session_factory, "5.º B")
    alumno = crear_alumno(session_factory, salon_a)
    with session_factory() as db:
        db.add(Matricula(alumno_id=alumno, salon_id=salon_b, anio_lectivo_id=datos["anio"]))
        with pytest.raises(IntegrityError):
            db.flush()


def test_alumno_nombre_se_lee_desde_persona(session_factory):
    salon = crear_salon(session_factory)
    alumno_id = crear_alumno(session_factory, salon, "Ana", "Ficticia Pérez")
    with session_factory() as db:
        assert db.get(Alumno, alumno_id).nombre == "Ana Ficticia Pérez"
        assert db.scalar(select(Alumno.id).where(Alumno.nombre.ilike("%ficticia%"))) == alumno_id


def _base(session_factory):
    salon = crear_salon(session_factory)
    alumno = crear_alumno(session_factory, salon)
    staff = crear_usuario(session_factory, rol="coordinacion")
    with session_factory() as db:
        familia = Familia(nombre_familia="Familia Ficticia")
        db.add(familia)
        db.commit()
        return salon, alumno, familia.id, staff.id


def test_una_respuesta_por_alumno_y_campana(session_factory):
    _, alumno, familia, staff = _base(session_factory)
    with session_factory() as db:
        campana = Campana(titulo="Salida", tipo="salida", texto_legal="Texto", texto_hash="h", fecha_limite=NOW,
                          creado_por=staff)
        db.add(campana)
        db.flush()
        datos = dict(campana_id=campana.id, alumno_id=alumno, familia_id=familia, firmante="Firmante",
                     parentesco="madre", decision="aceptada", canal="enlace", texto_hash="h")
        db.add(RespuestaAutorizacion(**datos))
        db.flush()
        db.add(RespuestaAutorizacion(**datos))
        with pytest.raises(IntegrityError):
            db.flush()


def test_recogida_requiere_adulto_o_marca_no_autorizado(session_factory):
    _, alumno, _, staff = _base(session_factory)
    with session_factory() as db:
        db.add(Recogida(alumno_id=alumno, adulto_id=None, no_autorizado=False, registrada_por=staff, ocurrida_en=NOW))
        with pytest.raises(IntegrityError):
            db.flush()


def test_sync_id_duplicado_se_rechaza(session_factory):
    _, alumno, _, staff = _base(session_factory)
    with session_factory() as db:
        datos = dict(alumno_id=alumno, no_autorizado=True, registrada_por=staff, ocurrida_en=NOW, sync_id="evento-1")
        db.add(Recogida(**datos))
        db.flush()
        db.add(Recogida(**datos))
        with pytest.raises(IntegrityError):
            db.flush()


def test_solo_un_codigo_activo_por_adulto_y_alumno(session_factory):
    _, alumno, familia, _ = _base(session_factory)
    with session_factory() as db:
        adulto = AdultoAutorizado(alumno_id=alumno, familia_id=familia, nombre="Adulto", parentesco="tía", estado="validado")
        db.add(adulto)
        db.flush()
        db.add(CodigoRecogida(adulto_id=adulto.id, alumno_id=alumno, codigo_hash="a", expira_en=NOW))
        db.flush()
        db.add(CodigoRecogida(adulto_id=adulto.id, alumno_id=alumno, codigo_hash="b", expira_en=NOW))
        with pytest.raises(IntegrityError):
            db.flush()


def test_codigo_usado_no_bloquea_uno_nuevo(session_factory):
    _, alumno, familia, _ = _base(session_factory)
    with session_factory() as db:
        adulto = AdultoAutorizado(alumno_id=alumno, familia_id=familia, nombre="Adulto", parentesco="tía", estado="validado")
        db.add(adulto)
        db.flush()
        db.add(CodigoRecogida(adulto_id=adulto.id, alumno_id=alumno, codigo_hash="a", expira_en=NOW, usado_en=NOW))
        db.flush()
        db.add(CodigoRecogida(adulto_id=adulto.id, alumno_id=alumno, codigo_hash="b", expira_en=NOW))
        db.flush()


# ---------- PostgreSQL: migración real y bitácora inmutable ----------

@pytest.fixture()
def postgres():
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Definir TEST_POSTGRES_URL para probar contra PostgreSQL")
    from alembic import command
    from alembic.config import Config

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    yield engine, cfg
    command.downgrade(cfg, "base")
    engine.dispose()


def test_bitacora_es_inmutable_en_postgres(postgres):
    engine, _ = postgres
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO bitacora_auditoria (accion, entidad) VALUES ('crear', 'alumno')"))
    with pytest.raises(Exception, match="inmutable"):
        with engine.begin() as conn:
            conn.execute(text("UPDATE bitacora_auditoria SET accion = 'x'"))
    with pytest.raises(Exception, match="inmutable"):
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM bitacora_auditoria"))


def test_migracion_coincide_con_el_modelo(postgres):
    """La migración genera exactamente el esquema que describen los modelos."""
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    engine, _ = postgres
    with engine.connect() as conn:
        diferencias = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diferencias == []


def test_indice_parcial_de_anio_activo_en_postgres(postgres):
    engine, _ = postgres
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO anios_lectivos (nombre, inicio, fin, activo) VALUES ('a', '2026-05-01', '2027-02-01', true)"))
        conn.execute(text("INSERT INTO anios_lectivos (nombre, inicio, fin, activo) VALUES ('b', '2027-05-01', '2028-02-01', false)"))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO anios_lectivos (nombre, inicio, fin, activo) VALUES ('c', '2028-05-01', '2029-02-01', true)"))
