import re
from datetime import timedelta

import pyotp
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import security
from app.models import AdultoAutorizado, Alumno, Campana, Familia, RespuestaAutorizacion, Salon
from app.tiempo import TZ_ESCUELA, ahora
from tests.conftest import PASSWORD, crear_usuario

CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')


def _csrf(client, url="/admin/login") -> str:
    return CSRF_RE.search(client.get(url).text).group(1)


def _entrar(client, email):
    return client.post("/admin/login", data={"email": email, "password": PASSWORD, "csrf": _csrf(client)},
                       follow_redirects=False)


def _panel(app, session_factory, rol="coordinadora"):
    client = TestClient(app)
    crear_usuario(session_factory, rol=rol, email=f"{rol}@ejemplo.test")
    r = _entrar(client, f"{rol}@ejemplo.test")
    assert r.status_code == 303
    return client


def _post(client, url, datos, **kwargs):
    """Envía un formulario del panel con el token CSRF de la sesión actual."""
    return client.post(url, data={**datos, "csrf": _csrf(client, "/admin")}, follow_redirects=False, **kwargs)


def _salon(session_factory, nombre="1A") -> int:
    with session_factory() as db:
        salon = Salon(nombre=nombre, grado="1ro")
        db.add(salon)
        db.commit()
        return salon.id


# ---------- Acceso ----------

def test_panel_sin_sesion_redirige_al_login(app):
    r = TestClient(app).get("/admin", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"


def test_raiz_lleva_al_panel(app):
    r = TestClient(app).get("/", follow_redirects=False)
    assert r.headers["location"] == "/admin"


def test_login_por_formulario_abre_el_panel(app, session_factory):
    client = _panel(app, session_factory)
    r = client.get("/admin")
    assert r.status_code == 200
    assert "Panel de coordinación" in r.text


def test_login_incorrecto_muestra_el_error(app, session_factory):
    client = TestClient(app)
    crear_usuario(session_factory, rol="coordinadora", email="c@ejemplo.test")
    r = client.post("/admin/login", data={"email": "c@ejemplo.test", "password": "mala", "csrf": _csrf(client)})
    assert r.status_code == 401
    assert "no válidos" in r.text


def test_login_sin_token_csrf_es_rechazado(app, session_factory):
    client = TestClient(app)
    crear_usuario(session_factory, rol="coordinadora", email="c@ejemplo.test")
    client.get("/admin/login")
    r = client.post("/admin/login", data={"email": "c@ejemplo.test", "password": PASSWORD})
    assert r.status_code == 403
    assert client.get("/admin", follow_redirects=False).status_code == 303  # sigue sin sesión


def test_login_con_totp(app, session_factory):
    secreto = security.new_totp_secret()
    crear_usuario(session_factory, rol="administrador", email="a@ejemplo.test", totp_secret=secreto)
    client = TestClient(app)
    r = _entrar(client, "a@ejemplo.test")
    assert r.headers["location"] == "/admin/totp"
    assert client.get("/admin", follow_redirects=False).status_code == 303  # aún sin acceso

    r = client.post("/admin/totp", data={"codigo": pyotp.TOTP(secreto).now(), "csrf": _csrf(client, "/admin/totp")},
                    follow_redirects=False)
    assert r.headers["location"] == "/admin"
    assert client.get("/admin").status_code == 200


def test_porteria_no_entra_al_panel(app, session_factory):
    client = _panel(app, session_factory, rol="porteria")
    r = client.get("/admin")
    assert r.status_code == 403
    assert "no tiene acceso" in r.text


def test_salir_cierra_la_sesion(app, session_factory):
    client = _panel(app, session_factory)
    _post(client, "/admin/logout", {})
    assert client.get("/admin", follow_redirects=False).status_code == 303


# ---------- Formularios ----------

def test_formulario_sin_csrf_no_crea_nada(app, session_factory):
    client = _panel(app, session_factory)
    r = client.post("/admin/salones", data={"nombre": "1A", "grado": "1ro"})
    assert r.status_code == 403
    with session_factory() as db:
        assert db.scalar(select(Salon)) is None


def test_crear_salon_y_alumno(app, session_factory):
    client = _panel(app, session_factory)
    assert _post(client, "/admin/salones", {"nombre": "1A", "grado": "1ro"}).status_code == 303
    with session_factory() as db:
        salon_id = db.scalar(select(Salon.id))
    _post(client, "/admin/alumnos", {"codigo_interno": "E-1", "nombre": "Alumno Ficticio", "salon_id": str(salon_id)})
    pagina = client.get("/admin/alumnos").text
    assert "Alumno Ficticio" in pagina
    assert "agregado" in pagina  # aviso de confirmación


def test_salon_duplicado_muestra_aviso(app, session_factory):
    client = _panel(app, session_factory)
    _post(client, "/admin/salones", {"nombre": "1A", "grado": "1ro"})
    _post(client, "/admin/salones", {"nombre": "1A", "grado": "1ro"})
    assert "Ya existe" in client.get("/admin/salones").text


def test_desactivar_alumno(app, session_factory):
    client = _panel(app, session_factory)
    salon_id = _salon(session_factory)
    with session_factory() as db:
        alumno = Alumno(codigo_interno="E-1", nombre="Alumno Ficticio", salon_id=salon_id)
        db.add(alumno)
        db.commit()
        alumno_id = alumno.id
    _post(client, f"/admin/alumnos/{alumno_id}/activo", {"activo": "0"})
    with session_factory() as db:
        assert db.get(Alumno, alumno_id).activo is False


# ---------- Importación ----------

CSV_PRUEBA = (
    "codigo_interno;nombre;salon\n"
    "E-1;Ana Ficticia;1A\n"
    "E-1;Repetida Ficticia;1A\n"
    "E-2;Sin Salón;9Z\n"
    "E-3;Beto Ficticio;1a\n"
    ";;\n"
)


def test_importacion_csv_con_vista_previa(app, session_factory):
    client = _panel(app, session_factory)
    _salon(session_factory, "1A")

    r = client.post("/admin/alumnos/importar/vista-previa",
                    data={"csrf": _csrf(client, "/admin/alumnos/importar")},
                    files={"archivo": ("alumnos.csv", CSV_PRUEBA.encode("utf-8"), "text/csv")})
    assert r.status_code == 200
    assert "Código repetido dentro del archivo" in r.text
    assert "no existe" in r.text
    with session_factory() as db:
        assert db.scalar(select(Alumno)) is None  # la vista previa no guarda nada

    texto = re.search(r'<textarea name="texto" hidden readonly>(.*?)</textarea>', r.text, re.S).group(1)
    import html
    _post(client, "/admin/alumnos/importar", {"texto": html.unescape(texto)})
    with session_factory() as db:
        codigos = sorted(db.scalars(select(Alumno.codigo_interno)).all())
    assert codigos == ["E-1", "E-3"]


def test_importacion_rechaza_columnas_faltantes(app, session_factory):
    client = _panel(app, session_factory)
    r = client.post("/admin/alumnos/importar/vista-previa",
                    data={"csrf": _csrf(client, "/admin/alumnos/importar")},
                    files={"archivo": ("x.csv", b"nombre,apellido\nAna,Ficticia\n", "text/csv")},
                    follow_redirects=True)
    assert "Faltan columnas" in r.text


def test_importacion_acepta_csv_de_excel_en_windows(app, session_factory):
    client = _panel(app, session_factory)
    _salon(session_factory, "1A")
    contenido = "codigo_interno;nombre;salon\r\nE-9;José Ñúñez;1A\r\n".encode("cp1252")
    r = client.post("/admin/alumnos/importar/vista-previa",
                    data={"csrf": _csrf(client, "/admin/alumnos/importar")},
                    files={"archivo": ("x.csv", contenido, "text/csv")})
    assert "José Ñúñez" in r.text


# ---------- Familias y personas autorizadas ----------

def test_generar_enlace_desde_el_panel(app, session_factory):
    client = _panel(app, session_factory)
    _post(client, "/admin/familias", {"nombre_familia": "Familia Ficticia", "telefono": "0991234567", "consentimiento": "on"})
    with session_factory() as db:
        familia = db.scalar(select(Familia))
        assert familia.consentimiento_mensajes is True
    r = _post(client, f"/admin/familias/{familia.id}/enlace", {})
    assert r.status_code == 200
    url = re.search(r'value="(http[^"]+/f/[^"]+)"', r.text).group(1)
    assert "https://wa.me/593991234567" in r.text
    ruta = url[url.index("/f/"):]
    assert TestClient(app).get(ruta).status_code == 200


def test_validar_persona_pendiente(app, session_factory):
    client = _panel(app, session_factory)
    salon_id = _salon(session_factory)
    with session_factory() as db:
        alumno = Alumno(codigo_interno="E-1", nombre="Alumno Ficticio", salon_id=salon_id)
        db.add(alumno)
        db.flush()
        adulto = AdultoAutorizado(alumno_id=alumno.id, nombre="Adulta Ficticia", parentesco="tía")
        db.add(adulto)
        db.commit()
        adulto_id = adulto.id
    assert "Adulta Ficticia" in client.get("/admin/adultos").text
    _post(client, f"/admin/adultos/{adulto_id}/estado", {"estado": "validado"})
    with session_factory() as db:
        assert db.get(AdultoAutorizado, adulto_id).estado == "validado"
    assert "Adulta Ficticia: validado." in client.get("/admin/adultos").text  # aviso de confirmación
    assert "Adulta Ficticia" not in client.get("/admin/adultos").text  # ya no figura entre las pendientes


# ---------- Campañas ----------

def _crear_campana(client, salon_id, dias=5):
    limite = (ahora() + timedelta(days=dias)).astimezone(TZ_ESCUELA).strftime("%Y-%m-%dT%H:%M")
    return _post(client, "/admin/campanas", {
        "titulo": "Salida ficticia", "tipo": "salida", "descripcion": "",
        "texto_legal": "Autorizo la salida.", "fecha_limite": limite, "salon_ids": [str(salon_id)],
    })


def test_campana_desde_el_panel_muestra_avance(app, session_factory):
    client = _panel(app, session_factory)
    salon_id = _salon(session_factory)
    with session_factory() as db:
        alumnos = [Alumno(codigo_interno=f"E-{i}", nombre=f"Alumno {i}", salon_id=salon_id) for i in range(4)]
        familia = Familia(nombre_familia="Familia Ficticia")
        db.add_all([*alumnos, familia])
        db.commit()
        primer_alumno, familia_id = alumnos[0].id, familia.id

    r = _crear_campana(client, salon_id)
    assert r.status_code == 303
    with session_factory() as db:
        campana = db.scalar(select(Campana))
        db.add(RespuestaAutorizacion(campana_id=campana.id, alumno_id=primer_alumno, familia_id=familia_id,
                                     firmante="Madre Ficticia", parentesco="madre", decision="aceptada",
                                     canal="enlace", texto_hash=campana.texto_hash))
        db.commit()

    pagina = client.get(r.headers["location"]).text
    assert "25 %" in pagina  # 1 de 4 respondió
    assert "Madre Ficticia" in pagina
    pendientes = client.get(f"{r.headers['location']}?estado=pendiente").text
    assert "Alumno 0" not in pendientes and "Alumno 3" in pendientes


def test_campana_con_fecha_pasada_se_rechaza(app, session_factory):
    client = _panel(app, session_factory)
    salon_id = _salon(session_factory)
    _crear_campana(client, salon_id, dias=-1)
    assert "debe ser futura" in client.get("/admin/campanas").text
    with session_factory() as db:
        assert db.scalar(select(Campana)) is None


def test_csv_de_campana_neutraliza_formulas(app, session_factory):
    client = _panel(app, session_factory)
    salon_id = _salon(session_factory)
    with session_factory() as db:
        alumno = Alumno(codigo_interno="E-1", nombre="Alumno Ficticio", salon_id=salon_id)
        familia = Familia(nombre_familia="Familia Ficticia")
        db.add_all([alumno, familia])
        db.commit()
        alumno_id, familia_id = alumno.id, familia.id
    r = _crear_campana(client, salon_id)
    campana_id = int(r.headers["location"].rsplit("/", 1)[1])
    with session_factory() as db:
        campana = db.get(Campana, campana_id)
        db.add(RespuestaAutorizacion(campana_id=campana_id, alumno_id=alumno_id, familia_id=familia_id,
                                     firmante='=HYPERLINK("http://malicioso")', parentesco="madre",
                                     decision="aceptada", canal="enlace", texto_hash=campana.texto_hash))
        db.commit()
    csv_texto = client.get(f"/admin/campanas/{campana_id}/csv").text
    assert "'=HYPERLINK" in csv_texto
    assert ";=HYPERLINK" not in csv_texto


def test_bitacora_muestra_acciones(app, session_factory):
    client = _panel(app, session_factory)
    _post(client, "/admin/salones", {"nombre": "1A", "grado": "1ro"})
    pagina = client.get("/admin/bitacora").text
    assert "inicio_sesion" in pagina
    assert "salon" in pagina
