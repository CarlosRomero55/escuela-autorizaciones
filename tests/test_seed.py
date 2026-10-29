import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import (
    AdultoAutorizado,
    Alumno,
    Grado,
    Matricula,
    Nivel,
    RepresentanteAlumno,
    Salon,
    SalonMateriaDocente,
    Usuario,
    UsuarioRol,
)
from app.seed import poblar
from tests.conftest import SETTINGS

PASSWORD = "Demo-Escuela-2026"


def _contar(db, modelo, *condiciones):
    return db.scalar(select(func.count()).select_from(modelo).where(*condiciones))


def test_poblar_crea_la_escuela_esperada(session_factory):
    with session_factory() as db:
        resumen = poblar(db, settings=SETTINGS, password=PASSWORD)
        assert _contar(db, Nivel) == 3
        assert _contar(db, Salon) == 6
        assert _contar(db, Alumno) == 120
        assert _contar(db, Matricula, Matricula.estado == "activa") == 120
        assert resumen["familias"] < 120  # hay hermanos
        # Todos los salones tienen tutor y todas sus materias tienen docente
        assert _contar(db, Salon, Salon.tutor_id.is_(None)) == 0
        assert _contar(db, SalonMateriaDocente, SalonMateriaDocente.docente_id.is_(None)) == 0
        # Cada alumno tiene un representante validado y a lo sumo dos adultos autorizados
        sin_rep = select(Alumno.id).where(Alumno.id.not_in(select(RepresentanteAlumno.alumno_id)))
        assert db.scalars(sin_rep).all() == []
        por_alumno = db.execute(select(AdultoAutorizado.alumno_id, func.count()).group_by(AdultoAutorizado.alumno_id)).all()
        assert all(c <= 2 for _, c in por_alumno)
        # Hay un usuario por cada rol
        roles = set(db.scalars(select(UsuarioRol.rol)))
        assert roles == {"administrador", "coordinacion", "directivo", "docente", "consejeria", "auditor",
                         "porteria", "representante", "estudiante"}


def test_solo_estudiantes_desde_quinto_tienen_cuenta(session_factory):
    with session_factory() as db:
        poblar(db, settings=SETTINGS, password=PASSWORD)
        ordenes = db.scalars(
            select(Grado.orden).join(Salon, Salon.grado_id == Grado.id).join(Alumno, Alumno.salon_id == Salon.id)
            .join(Usuario, Usuario.persona_id == Alumno.persona_id)
        ).all()
    assert ordenes and min(ordenes) >= SETTINGS.grado_minimo_cuenta_estudiante


def test_personal_con_totp_obligatorio_lo_tiene_configurado(session_factory):
    with session_factory() as db:
        poblar(db, settings=SETTINGS, password=PASSWORD)
        for login in ("admin", "sofia", "rector", "docente", "consejeria", "auditor"):
            assert db.scalar(select(Usuario).where(Usuario.login == login)).totp_secret_cifrado


def test_poblar_no_sobrescribe_datos_existentes(session_factory):
    with session_factory() as db:
        poblar(db, settings=SETTINGS, password=PASSWORD)
        with pytest.raises(RuntimeError):
            poblar(db, settings=SETTINGS, password=PASSWORD)


def test_enlaces_de_familia_de_demo_funcionan(app, session_factory):
    with session_factory() as db:
        resumen = poblar(db, settings=SETTINGS, password=PASSWORD)
    for _, url in resumen["enlaces"]:
        assert TestClient(app).get(url).status_code == 200
