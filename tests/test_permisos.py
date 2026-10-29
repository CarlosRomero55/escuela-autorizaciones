"""Reglas de acceso por rol y por relación real con los datos.

Cada intento de acceso cruzado debe fallar con la misma respuesta que un recurso inexistente.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Alumno, BitacoraAuditoria, RepresentanteAlumno, Usuario, UsuarioRol
from app.permisos import alumnos_visibles, construir_actor, puede, salones_visibles
from tests.conftest import (
    asignar,
    crear_alumno,
    crear_salon,
    crear_usuario,
    entrar_web,
    estructura,
    post_web,
    vincular_representante,
)

NO_ENCONTRADO = "No encontramos lo que busca, o no está disponible para su usuario."


@pytest.fixture()
def escuela(session_factory):
    """Dos salones con docentes distintos, un alumno propio y uno ajeno en cada salón."""
    sf = session_factory
    estructura(sf)
    docente_a = crear_usuario(sf, rol="docente")
    docente_b = crear_usuario(sf, rol="docente")
    salon_a = crear_salon(sf, "5.º A", tutor_id=docente_a.id)
    salon_b = crear_salon(sf, "3.º A", tutor_id=docente_b.id)
    asignar(sf, salon_a, "Matemática", docente_a.id)
    asignar(sf, salon_b, "Lengua y Literatura", docente_b.id)
    hijo = crear_alumno(sf, salon_a, "Hijo", "Representado")
    companero = crear_alumno(sf, salon_a, "Compañero", "De Salón")
    otro = crear_alumno(sf, salon_b, "Otro", "Salón Ajeno")
    representante = crear_usuario(sf, rol="representante")
    vincular_representante(sf, representante.id, hijo)
    return {"docente_a": docente_a, "docente_b": docente_b, "salon_a": salon_a, "salon_b": salon_b,
            "hijo": hijo, "companero": companero, "otro": otro, "representante": representante}


def _no_encontrado(respuesta) -> bool:
    return respuesta.status_code == 404 and NO_ENCONTRADO in respuesta.text


# ---------- Representante ----------

def test_representante_ve_a_su_representado(app, escuela):
    c = entrar_web(app, escuela["representante"])
    r = c.get(f"/portal/alumnos/{escuela['hijo']}")
    assert r.status_code == 200 and "Hijo Representado" in r.text


def test_representante_no_ve_a_otro_alumno_ni_se_revela_su_existencia(app, escuela):
    c = entrar_web(app, escuela["representante"])
    ajeno = c.get(f"/portal/alumnos/{escuela['companero']}")
    inexistente = c.get("/portal/alumnos/99999")
    assert _no_encontrado(ajeno) and _no_encontrado(inexistente)
    assert "Compañero" not in ajeno.text
    assert ajeno.text == inexistente.text  # misma respuesta: no revela que el alumno existe


def test_representante_no_ve_el_listado_del_salon(app, escuela):
    c = entrar_web(app, escuela["representante"])
    r = c.get(f"/portal/salones/{escuela['salon_a']}")
    assert r.status_code == 200
    assert "Compañero" not in r.text


def test_vinculo_sin_validar_no_da_acceso(app, session_factory, escuela):
    rep = crear_usuario(session_factory, rol="representante")
    with session_factory() as db:
        db.add(RepresentanteAlumno(usuario_id=rep.id, alumno_id=escuela["otro"], parentesco="padre"))
        db.commit()
    assert _no_encontrado(entrar_web(app, rep).get(f"/portal/alumnos/{escuela['otro']}"))


# ---------- Estudiante ----------

def test_estudiante_solo_ve_su_ficha_y_su_salon(app, session_factory, escuela):
    with session_factory() as db:
        persona_hijo = db.get(Alumno, escuela["hijo"]).persona_id
    estudiante = crear_usuario(session_factory, rol="estudiante", persona_id=persona_hijo)
    c = entrar_web(app, estudiante)
    assert c.get(f"/portal/alumnos/{escuela['hijo']}").status_code == 200
    assert _no_encontrado(c.get(f"/portal/alumnos/{escuela['companero']}"))
    assert _no_encontrado(c.get(f"/portal/salones/{escuela['salon_b']}"))
    salon = c.get(f"/portal/salones/{escuela['salon_a']}")
    # Ve a sus compañeros solo por nombre, sin enlace a su ficha
    assert "Compañero De Salón" in salon.text
    assert f"/portal/alumnos/{escuela['companero']}" not in salon.text


# ---------- Docente ----------

def test_docente_ve_alumnos_de_sus_salones_y_no_de_otros(app, escuela):
    c = entrar_web(app, escuela["docente_a"])
    assert c.get(f"/portal/alumnos/{escuela['companero']}").status_code == 200
    assert _no_encontrado(c.get(f"/portal/alumnos/{escuela['otro']}"))
    assert _no_encontrado(c.get(f"/portal/salones/{escuela['salon_b']}"))


def test_docente_no_entra_a_la_administracion(app, escuela):
    c = entrar_web(app, escuela["docente_a"])
    for url in ("/admin/usuarios", "/admin/estructura", "/admin/campanas", "/admin/bitacora"):
        assert c.get(url).status_code == 403


# ---------- Directivo ----------

def test_directivo_de_area_solo_ve_salones_con_materias_de_su_area(app, session_factory, escuela):
    datos = estructura(session_factory)
    directivo = crear_usuario(session_factory, rol="directivo", area_id=datos["areas"]["Matemática"])
    c = entrar_web(app, directivo)
    assert c.get(f"/portal/salones/{escuela['salon_a']}").status_code == 200
    assert _no_encontrado(c.get(f"/portal/salones/{escuela['salon_b']}"))
    assert _no_encontrado(c.get(f"/portal/alumnos/{escuela['otro']}"))


def test_directivo_institucional_ve_todos_los_salones(app, session_factory, escuela):
    c = entrar_web(app, crear_usuario(session_factory, rol="directivo"))
    assert c.get(f"/portal/salones/{escuela['salon_b']}").status_code == 200
    assert c.get(f"/portal/alumnos/{escuela['otro']}").status_code == 200


# ---------- Consejería, auditoría y portería ----------

@pytest.mark.parametrize("rol", ["consejeria", "auditor", "porteria"])
def test_roles_sin_alcance_academico_no_ven_fichas(app, session_factory, escuela, rol):
    c = entrar_web(app, crear_usuario(session_factory, rol=rol))
    assert _no_encontrado(c.get(f"/portal/alumnos/{escuela['hijo']}"))
    assert _no_encontrado(c.get(f"/portal/salones/{escuela['salon_a']}"))


def test_auditor_lee_la_bitacora_y_su_consulta_queda_registrada(app, session_factory, escuela):
    auditor = crear_usuario(session_factory, rol="auditor")
    c = entrar_web(app, auditor)
    assert c.get("/admin/bitacora").status_code == 200
    with session_factory() as db:
        acciones = db.scalars(select(BitacoraAuditoria.accion).where(BitacoraAuditoria.usuario_id == auditor.id)).all()
    assert "consultar_bitacora" in acciones
    assert post_web(c, "/admin/estructura/areas", {"nombre": "Nueva"}).status_code == 403


# ---------- Administrador: motivo obligatorio ----------

def test_administrador_debe_justificar_el_acceso_a_un_alumno(app, session_factory, escuela):
    admin = crear_usuario(session_factory, rol="administrador")
    c = entrar_web(app, admin)
    r = c.get(f"/portal/alumnos/{escuela['hijo']}")
    assert "Motivo" in r.text and "Hijo Representado" in r.text  # pide el motivo antes de mostrar datos
    assert "Historial de matrículas" not in r.text

    assert post_web(c, f"/portal/alumnos/{escuela['hijo']}/acceso", {"motivo": "corto"}).status_code == 303
    assert "Historial de matrículas" not in c.get(f"/portal/alumnos/{escuela['hijo']}").text

    post_web(c, f"/portal/alumnos/{escuela['hijo']}/acceso", {"motivo": "Corregir salón por solicitud de secretaría"})
    assert "Historial de matrículas" in c.get(f"/portal/alumnos/{escuela['hijo']}").text
    # El acceso es solo para ese alumno
    assert "Historial de matrículas" not in c.get(f"/portal/alumnos/{escuela['otro']}").text
    with session_factory() as db:
        entrada = db.scalar(select(BitacoraAuditoria).where(BitacoraAuditoria.accion == "acceso_datos_alumno"))
    assert entrada.motivo == "Corregir salón por solicitud de secretaría"
    assert entrada.entidad_id == escuela["hijo"] and entrada.rol == "administrador"


# ---------- Búsqueda ----------

def test_busqueda_no_revela_alumnos_fuera_de_alcance(app, escuela):
    c = entrar_web(app, escuela["representante"])
    r = c.get("/portal/buscar?q=Ajeno", headers={"HX-Request": "true"})
    assert "Otro Salón Ajeno" not in r.text and "Sin resultados" in r.text
    assert "Hijo Representado" in c.get("/portal/buscar?q=Hijo", headers={"HX-Request": "true"}).text


def test_busqueda_del_docente_se_limita_a_sus_salones(app, escuela):
    c = entrar_web(app, escuela["docente_a"])
    assert "Compañero De Salón" in c.get("/portal/buscar?q=Compa").text
    assert "Otro Salón Ajeno" not in c.get("/portal/buscar?q=Otro").text


# ---------- Rol activo ----------

def test_cambio_de_rol_cambia_lo_que_se_ve(app, session_factory, escuela):
    docente_madre = crear_usuario(session_factory, roles=["docente", "representante"])
    vincular_representante(session_factory, docente_madre.id, escuela["otro"])
    c = entrar_web(app, docente_madre)
    # Como docente (rol inicial) no tiene salones: no ve al alumno de 3.º A
    assert _no_encontrado(c.get(f"/portal/alumnos/{escuela['otro']}"))
    post_web(c, "/portal/rol", {"rol": "representante"})
    assert c.get(f"/portal/alumnos/{escuela['otro']}").status_code == 200


def test_no_se_puede_activar_un_rol_que_no_se_tiene(app, escuela):
    c = entrar_web(app, escuela["representante"])
    post_web(c, "/portal/rol", {"rol": "administrador"})
    assert c.get("/admin/usuarios").status_code == 403


# ---------- Unidad: construcción del actor ----------

def test_rol_vencido_no_cuenta(session_factory):
    cuenta = crear_usuario(session_factory, roles=["docente", "administrador"])
    with session_factory() as db:
        rol = db.scalar(select(UsuarioRol).where(UsuarioRol.usuario_id == cuenta.id, UsuarioRol.rol == "administrador"))
        rol.vigente_hasta = datetime.now(UTC) - timedelta(days=1)
        db.commit()
        actor = construir_actor(db.get(Usuario, cuenta.id), "administrador")
    assert actor.rol == "docente" and actor.roles == ("docente",)
    assert not puede(actor, "usuarios.gestionar")


def test_alcance_de_portería_es_vacio_en_el_portal(session_factory, escuela):
    cuenta = crear_usuario(session_factory, rol="porteria")
    with session_factory() as db:
        actor = construir_actor(db.get(Usuario, cuenta.id), None)
        assert db.scalars(alumnos_visibles(db, actor)).all() == []
        assert db.scalars(salones_visibles(db, actor)).all() == []
