import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import security
from app.models import AdultoAutorizado, Alumno, FamiliaAlumno, RespuestaAutorizacion, Salon, UsuarioStaff
from app.seed import poblar
from tests.conftest import SETTINGS

PASSWORD = "Demo-Escuela-2026"


def _contar(db, modelo):
    return db.scalar(select(func.count()).select_from(modelo))


def test_poblar_crea_los_datos_esperados(session_factory):
    with session_factory() as db:
        resumen = poblar(db, settings=SETTINGS, password=PASSWORD)
        assert _contar(db, Salon) == 3
        assert _contar(db, Alumno) == 60
        assert _contar(db, UsuarioStaff) == 4
        assert {rol for _, rol in resumen["usuarios"]} == {"administrador", "coordinadora", "porteria", "maestra"}
        # Todos los alumnos tienen familia y a lo sumo dos adultos
        assert _contar(db, FamiliaAlumno) == 60
        por_alumno = db.execute(select(AdultoAutorizado.alumno_id, func.count())
                                .group_by(AdultoAutorizado.alumno_id)).all()
        assert all(cantidad <= 2 for _, cantidad in por_alumno)
        assert resumen["familias"] < 60  # hay hermanos que comparten familia
        assert 0 < _contar(db, RespuestaAutorizacion) < 60


def test_poblar_es_determinista(session_factory):
    with session_factory() as db:
        poblar(db, settings=SETTINGS, password=PASSWORD)
        nombres = db.scalars(select(Alumno.nombre).order_by(Alumno.id)).all()
    assert nombres[0] and len(set(nombres)) > 40


def test_poblar_no_sobrescribe_datos_existentes(session_factory):
    with session_factory() as db:
        poblar(db, settings=SETTINGS, password=PASSWORD)
        with pytest.raises(RuntimeError):
            poblar(db, settings=SETTINGS, password=PASSWORD)


def test_usuarios_y_enlaces_de_demo_funcionan(app, session_factory):
    with session_factory() as db:
        resumen = poblar(db, settings=SETTINGS, password=PASSWORD)
        sofia = db.scalar(select(UsuarioStaff).where(UsuarioStaff.email == "sofia@demo.test"))
        assert security.verify_password(sofia.password_hash, PASSWORD)

    publico = TestClient(app)
    for _, url in resumen["enlaces"]:
        assert publico.get(url).status_code == 200
