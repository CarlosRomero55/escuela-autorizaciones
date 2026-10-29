"""Panel de administración por formularios HTML."""

import html
import re
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import (
    AdultoAutorizado,
    Alumno,
    AnioLectivo,
    BitacoraAuditoria,
    Campana,
    Familia,
    Matricula,
    RepresentanteAlumno,
    RespuestaAutorizacion,
    Salon,
    SalonMateriaDocente,
    Usuario,
)
from app.tiempo import TZ_ESCUELA, ahora
from tests.conftest import (
    crear_alumno,
    crear_salon,
    crear_usuario,
    csrf_de,
    entrar_web,
    estructura,
    post_web,
)


def _admin(app, sf):
    return entrar_web(app, crear_usuario(sf, rol="administrador"))


# ---------- Acceso ----------

def test_portal_sin_sesion_redirige_al_ingreso(app):
    assert TestClient(app).get("/portal", follow_redirects=False).headers["location"] == "/ingresar"


def test_raiz_y_ruta_antigua_llevan_al_portal(app):
    c = TestClient(app)
    assert c.get("/", follow_redirects=False).headers["location"] == "/portal"
    assert c.get("/admin/login", follow_redirects=False).headers["location"] == "/ingresar"


def test_ingreso_incorrecto_muestra_error(app, session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    c = TestClient(app)
    r = c.post("/ingresar", data={"identificador": cuenta.login, "password": "mala", "csrf": csrf_de(c, "/ingresar")})
    assert r.status_code == 401 and "No se pudo iniciar sesión" in r.text


def test_menu_cambia_segun_el_rol(app, session_factory):
    admin = _admin(app, session_factory).get("/portal").text
    assert "Estructura académica" in admin and "Usuarios" in admin and "Bitácora" in admin
    coordinacion = entrar_web(app, crear_usuario(session_factory, rol="coordinacion")).get("/portal").text
    assert "Campañas" in coordinacion and "Usuarios" not in coordinacion and "Bitácora" not in coordinacion
    docente = entrar_web(app, crear_usuario(session_factory, rol="docente")).get("/portal").text
    assert "Campañas" not in docente and "Mis clases" in docente


def test_formulario_sin_csrf_no_crea_nada(app, session_factory):
    estructura(session_factory)
    c = _admin(app, session_factory)
    r = c.post("/admin/estructura/areas", data={"nombre": "Robótica"})
    assert r.status_code == 403 and "Formulario vencido" in r.text


# ---------- Estructura académica ----------

def test_crear_y_activar_anio_lectivo(app, session_factory):
    estructura(session_factory)
    c = _admin(app, session_factory)
    post_web(c, "/admin/estructura/anios", {"nombre": "2027-2028", "inicio": "2027-05-03", "fin": "2028-02-04"})
    with session_factory() as db:
        nuevo = db.scalar(select(AnioLectivo).where(AnioLectivo.nombre == "2027-2028"))
        assert nuevo.activo is False
    post_web(c, f"/admin/estructura/anios/{nuevo.id}/activar")
    with session_factory() as db:
        activos = db.scalars(select(AnioLectivo.nombre).where(AnioLectivo.activo.is_(True))).all()
    assert activos == ["2027-2028"]


def test_periodo_fuera_del_anio_se_rechaza(app, session_factory):
    estructura(session_factory)
    c = _admin(app, session_factory)
    post_web(c, "/admin/estructura/periodos", {"nombre": "Fuera", "inicio": "2025-01-01", "fin": "2025-03-01"})
    assert "dentro de las fechas" in c.get("/admin/estructura").text


def test_crear_salon_asignar_tutor_y_docente(app, session_factory):
    datos = estructura(session_factory)
    docente = crear_usuario(session_factory, rol="docente")
    c = _admin(app, session_factory)
    r = post_web(c, "/admin/salones", {"grado_id": datos["grados"]["5.º"], "paralelo_id": datos["paralelos"]["B"],
                                       "tutor_id": str(docente.id)})
    salon_url = r.headers["location"]
    salon_id = int(salon_url.rsplit("/", 1)[1])
    post_web(c, f"{salon_url}/asignaciones", {"materia_id": datos["materias"]["Matemática"], "docente_id": str(docente.id)})
    with session_factory() as db:
        salon = db.get(Salon, salon_id)
        assert salon.nombre == "5.º B" and salon.tutor_id == docente.id
        asignacion = db.scalar(select(SalonMateriaDocente).where(SalonMateriaDocente.salon_id == salon_id))
        assert asignacion.docente_id == docente.id


def test_solo_un_docente_puede_ser_asignado(app, session_factory):
    datos = estructura(session_factory)
    salon = crear_salon(session_factory)
    representante = crear_usuario(session_factory, rol="representante")
    c = _admin(app, session_factory)
    post_web(c, f"/admin/salones/{salon}/asignaciones",
             {"materia_id": datos["materias"]["Matemática"], "docente_id": str(representante.id)})
    assert "no tiene rol docente" in c.get(f"/admin/salones/{salon}").text


def test_matricular_alumno_sin_salon(app, session_factory):
    datos = estructura(session_factory)
    salon_a = crear_salon(session_factory, "5.º A")
    salon_b = crear_salon(session_factory, "5.º B")
    alumno = crear_alumno(session_factory, salon_a)
    c = _admin(app, session_factory)
    post_web(c, f"/admin/alumnos/{alumno}/activo", {"activo": "0"})  # retira la matrícula
    post_web(c, f"/admin/salones/{salon_b}/matriculas", {"alumno_id": str(alumno)})
    with session_factory() as db:
        matricula = db.scalar(select(Matricula).where(Matricula.alumno_id == alumno, Matricula.anio_lectivo_id == datos["anio"]))
        assert matricula.salon_id == salon_b and matricula.estado == "activa"
        assert db.get(Alumno, alumno).salon_id == salon_b


# ---------- Alumnos e importación ----------

def test_crear_alumno_lo_matricula(app, session_factory):
    salon = crear_salon(session_factory)
    c = _admin(app, session_factory)
    post_web(c, "/admin/alumnos", {"codigo_interno": "E-1", "nombres": "Ana", "apellidos": "Ficticia", "salon_id": str(salon)})
    assert "Ana Ficticia" in c.get("/admin/alumnos").text
    with session_factory() as db:
        assert db.scalar(select(Matricula.salon_id)) == salon


CSV_PRUEBA = (
    "codigo_interno;nombres;apellidos;salon\n"
    "E-1;Ana;Ficticia;5.º A\n"
    "E-1;Repetida;Ficticia;5.º A\n"
    "E-2;Sin;Salón;9.º Z\n"
    "E-3;Beto;Ficticio;5.º a\n"
    ";;;\n"
)


def test_importacion_csv_con_vista_previa(app, session_factory):
    salon = crear_salon(session_factory, "5.º A")
    c = _admin(app, session_factory)
    r = c.post("/admin/alumnos/importar/vista-previa", data={"csrf": csrf_de(c, "/admin/alumnos/importar")},
               files={"archivo": ("alumnos.csv", CSV_PRUEBA.encode("utf-8"), "text/csv")})
    assert "Código repetido dentro del archivo" in r.text and "no existe en el año activo" in r.text
    with session_factory() as db:
        assert db.scalar(select(Alumno)) is None  # la vista previa no guarda nada
    texto = re.search(r'<textarea name="texto" hidden readonly>(.*?)</textarea>', r.text, re.S).group(1)
    post_web(c, "/admin/alumnos/importar", {"texto": html.unescape(texto)})
    with session_factory() as db:
        assert sorted(db.scalars(select(Alumno.codigo_interno)).all()) == ["E-1", "E-3"]
        assert set(db.scalars(select(Matricula.salon_id)).all()) == {salon}


def test_importacion_acepta_csv_de_excel_en_windows(app, session_factory):
    crear_salon(session_factory, "5.º A")
    c = _admin(app, session_factory)
    contenido = "codigo_interno;nombres;apellidos;salon\r\nE-9;José;Ñúñez;5.º A\r\n".encode("cp1252")
    r = c.post("/admin/alumnos/importar/vista-previa", data={"csrf": csrf_de(c, "/admin/alumnos/importar")},
               files={"archivo": ("x.csv", contenido, "text/csv")})
    assert "Ñúñez" in r.text


def test_cuenta_de_estudiante_solo_desde_quinto(app, session_factory):
    menor = crear_alumno(session_factory, crear_salon(session_factory, "3.º A"))
    mayor = crear_alumno(session_factory, crear_salon(session_factory, "5.º A"), "Ana", "Mayor")
    c = _admin(app, session_factory)
    r = post_web(c, f"/admin/alumnos/{menor}/cuenta")
    assert r.status_code == 303
    r = post_web(c, f"/admin/alumnos/{mayor}/cuenta")
    assert r.status_code == 200 and "/cuenta/activar/" in r.text
    with session_factory() as db:
        cuentas = db.scalars(select(Usuario)).all()
        alumno_mayor = db.get(Alumno, mayor)
    assert any(u.persona_id == alumno_mayor.persona_id for u in cuentas)
    assert not any(u.persona_id == db.get(Alumno, menor).persona_id for u in cuentas)


# ---------- Usuarios ----------

def test_alta_de_usuario_con_directivo_de_area(app, session_factory):
    datos = estructura(session_factory)
    c = _admin(app, session_factory)
    r = post_web(c, "/admin/usuarios", {"nombres": "Gloria", "apellidos": "Franco", "email": "gloria@ejemplo.test",
                                        "login": "", "roles": ["directivo"], "area_id": str(datos["areas"]["Matemática"])})
    assert r.status_code == 200 and "gloria.franco" in r.text
    with session_factory() as db:
        usuario = db.scalar(select(Usuario).where(Usuario.login == "gloria.franco"))
        assert usuario.password_hash is None
        assert [(x.rol, x.area_id) for x in usuario.roles] == [("directivo", datos["areas"]["Matemática"])]


def test_no_se_crean_estudiantes_desde_el_alta_de_usuarios(app, session_factory):
    c = _admin(app, session_factory)
    post_web(c, "/admin/usuarios", {"nombres": "X", "apellidos": "Y", "email": "", "login": "", "roles": ["estudiante"],
                                    "area_id": ""})
    with session_factory() as db:
        assert db.scalar(select(Usuario).where(Usuario.login == "x.y")) is None


def test_vincular_representante_desde_el_panel(app, session_factory):
    alumno = crear_alumno(session_factory, crear_salon(session_factory))
    rep = crear_usuario(session_factory, rol="representante")
    sin_rol = crear_usuario(session_factory, rol="docente")
    c = _admin(app, session_factory)
    post_web(c, f"/admin/usuarios/{sin_rol.id}/representados", {"alumno_id": str(alumno), "parentesco": "padre"})
    post_web(c, f"/admin/usuarios/{rep.id}/representados", {"alumno_id": str(alumno), "parentesco": "madre"})
    with session_factory() as db:
        vinculos = db.scalars(select(RepresentanteAlumno)).all()
    assert [(v.usuario_id, v.validado_en is not None) for v in vinculos] == [(rep.id, True)]
    # Ahora el representante ve al alumno
    assert entrar_web(app, rep).get(f"/portal/alumnos/{alumno}").status_code == 200


def test_desactivar_usuario_cierra_sus_sesiones(app, session_factory):
    rep = crear_usuario(session_factory, rol="representante")
    sesion_rep = entrar_web(app, rep)
    c = _admin(app, session_factory)
    post_web(c, f"/admin/usuarios/{rep.id}/activo", {"activo": "0"})
    assert sesion_rep.get("/portal", follow_redirects=False).status_code == 303


def test_el_administrador_no_puede_quitarse_su_propio_rol(app, session_factory):
    cuenta = crear_usuario(session_factory, rol="administrador")
    c = entrar_web(app, cuenta)
    with session_factory() as db:
        rol_id = db.get(Usuario, cuenta.id).roles[0].id
    post_web(c, f"/admin/usuarios/{cuenta.id}/roles/{rol_id}/quitar")
    with session_factory() as db:
        assert [r.rol for r in db.get(Usuario, cuenta.id).roles] == ["administrador"]


def test_listado_enmascara_correos(app, session_factory):
    crear_usuario(session_factory, rol="representante", email="maria.perez@ejemplo.test")
    pagina = _admin(app, session_factory).get("/admin/usuarios").text
    assert "maria.perez@ejemplo.test" not in pagina and "@ejemplo.test" in pagina


# ---------- Autorizaciones ----------

def _crear_campana(c, salon_id, dias=5):
    limite = (ahora() + timedelta(days=dias)).astimezone(TZ_ESCUELA).strftime("%Y-%m-%dT%H:%M")
    return post_web(c, "/admin/campanas", {"titulo": "Salida ficticia", "tipo": "salida", "descripcion": "",
                                            "texto_legal": "Autorizo la salida.", "fecha_limite": limite,
                                            "salon_ids": [str(salon_id)]})


def test_coordinacion_crea_campana_y_ve_avance(app, session_factory):
    salon = crear_salon(session_factory)
    alumnos = [crear_alumno(session_factory, salon, f"Alumno{i}") for i in range(4)]
    with session_factory() as db:
        familia = Familia(nombre_familia="Familia Ficticia")
        db.add(familia)
        db.commit()
        familia_id = familia.id
    c = entrar_web(app, crear_usuario(session_factory, rol="coordinacion"))
    r = _crear_campana(c, salon)
    with session_factory() as db:
        campana = db.scalar(select(Campana))
        db.add(RespuestaAutorizacion(campana_id=campana.id, alumno_id=alumnos[0], familia_id=familia_id,
                                     firmante="Madre Ficticia", parentesco="madre", decision="aceptada",
                                     canal="enlace", texto_hash=campana.texto_hash))
        db.commit()
    pagina = c.get(r.headers["location"]).text
    assert "25 %" in pagina and "Madre Ficticia" in pagina


def test_campana_con_fecha_pasada_se_rechaza(app, session_factory):
    salon = crear_salon(session_factory)
    c = entrar_web(app, crear_usuario(session_factory, rol="coordinacion"))
    _crear_campana(c, salon, dias=-1)
    assert "debe ser futura" in c.get("/admin/campanas").text
    with session_factory() as db:
        assert db.scalar(select(Campana)) is None


def test_csv_de_campana_neutraliza_formulas(app, session_factory):
    salon = crear_salon(session_factory)
    alumno = crear_alumno(session_factory, salon)
    with session_factory() as db:
        familia = Familia(nombre_familia="Familia Ficticia")
        db.add(familia)
        db.commit()
        familia_id = familia.id
    c = entrar_web(app, crear_usuario(session_factory, rol="coordinacion"))
    campana_id = int(_crear_campana(c, salon).headers["location"].rsplit("/", 1)[1])
    with session_factory() as db:
        campana = db.get(Campana, campana_id)
        db.add(RespuestaAutorizacion(campana_id=campana_id, alumno_id=alumno, familia_id=familia_id,
                                     firmante='=HYPERLINK("http://malicioso")', parentesco="madre",
                                     decision="aceptada", canal="enlace", texto_hash=campana.texto_hash))
        db.commit()
    texto = c.get(f"/admin/campanas/{campana_id}/csv").text
    assert "'=HYPERLINK" in texto and ";=HYPERLINK" not in texto


def test_validar_persona_autorizada(app, session_factory):
    alumno = crear_alumno(session_factory, crear_salon(session_factory))
    with session_factory() as db:
        adulto = AdultoAutorizado(alumno_id=alumno, nombre="Adulta Ficticia", parentesco="tía")
        db.add(adulto)
        db.commit()
        adulto_id = adulto.id
    c = entrar_web(app, crear_usuario(session_factory, rol="coordinacion"))
    assert "Adulta Ficticia" in c.get("/admin/adultos").text
    post_web(c, f"/admin/adultos/{adulto_id}/estado", {"estado": "validado"})
    with session_factory() as db:
        assert db.get(AdultoAutorizado, adulto_id).estado == "validado"


def test_generar_enlace_de_familia(app, session_factory):
    c = entrar_web(app, crear_usuario(session_factory, rol="coordinacion"))
    post_web(c, "/admin/familias", {"nombre_familia": "Familia Ficticia", "telefono": "0991234567", "email": "",
                                    "consentimiento": "on"})
    with session_factory() as db:
        familia = db.scalar(select(Familia))
    r = post_web(c, f"/admin/familias/{familia.id}/enlace")
    url = re.search(r'value="(http[^"]+/f/[^"]+)"', r.text).group(1)
    assert "https://wa.me/593991234567" in r.text
    assert TestClient(app).get(url[url.index("/f/"):]).status_code == 200


def test_bitacora_registra_acciones_con_rol(app, session_factory):
    estructura(session_factory)
    c = _admin(app, session_factory)
    post_web(c, "/admin/estructura/areas", {"nombre": "Robótica"})
    pagina = c.get("/admin/bitacora").text
    assert "inicio_sesion" in pagina and "Administración" in pagina
    with session_factory() as db:
        assert db.scalar(select(BitacoraAuditoria.rol).where(BitacoraAuditoria.entidad == "area")) == "administrador"
