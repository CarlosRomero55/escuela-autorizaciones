"""Cuentas: activación, recuperación, política de contraseñas y segundo factor."""

import re
from datetime import UTC, datetime, timedelta

import pyotp
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import crypto, security
from app.models import EnvioSaliente, TokenCuenta, Usuario
from app.security import validar_password
from tests.conftest import PASSWORD, SETTINGS, crear_usuario, csrf_de, entrar_web, iniciar, post_web

ENLACE_RE = re.compile(r'value="(http[^"]+/cuenta/(?:activar|restablecer)/[^"]+)"')


def _ruta(url: str) -> str:
    return url[url.index("/cuenta/"):]


# ---------- Política de contraseñas ----------

def test_politica_de_contraseñas():
    assert validar_password("corta") != []
    assert validar_password("aaaaaaaaaaaa") != []  # poco variada
    assert validar_password("contraseña") != []  # común y corta
    assert validar_password("maria.perez-2026", login="maria.perez") != []
    assert validar_password("Una frase bastante larga") == []


# ---------- Activación ----------

def _crear_por_panel(app, session_factory, roles=("docente",), email="nueva@ejemplo.test"):
    admin = entrar_web(app, crear_usuario(session_factory, rol="administrador"))
    r = post_web(admin, "/admin/usuarios", {"nombres": "Nueva", "apellidos": "Persona", "email": email, "login": "",
                                            "roles": list(roles), "area_id": ""})
    assert r.status_code == 200, r.text
    return admin, ENLACE_RE.search(r.text).group(1)


def test_alta_de_usuario_genera_enlace_de_activacion_de_un_solo_uso(app, session_factory):
    _, url = _crear_por_panel(app, session_factory, roles=("representante",))
    publico = TestClient(app)
    ruta = _ruta(url)
    pagina = publico.get(ruta)
    assert pagina.status_code == 200 and "nueva.persona" in pagina.text  # muestra el usuario sugerido
    r = publico.post(ruta, data={"password": "Mi frase de acceso", "confirmacion": "Mi frase de acceso",
                                 "csrf": csrf_de(publico, ruta)})
    assert "Contraseña guardada" in r.text
    assert publico.get(ruta).status_code == 404  # ya se usó
    assert iniciar(TestClient(app), "nueva@ejemplo.test", "Mi frase de acceso").json()["paso"] == "completo"


def test_activacion_rechaza_contraseña_debil_y_no_consume_el_enlace(app, session_factory):
    _, url = _crear_por_panel(app, session_factory)
    publico = TestClient(app)
    ruta = _ruta(url)
    r = publico.post(ruta, data={"password": "corta", "confirmacion": "corta", "csrf": csrf_de(publico, ruta)})
    assert r.status_code == 422 and "al menos 10" in r.text
    assert publico.get(ruta).status_code == 200


def test_enlace_de_activacion_vencido(app, session_factory):
    _, url = _crear_por_panel(app, session_factory)
    with session_factory() as db:
        token = db.scalar(select(TokenCuenta))
        token.expira_en = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()
    assert TestClient(app).get(_ruta(url)).status_code == 404


def test_el_token_de_cuenta_se_guarda_como_hash(app, session_factory):
    _, url = _crear_por_panel(app, session_factory)
    token = url.rsplit("/", 1)[1]
    with session_factory() as db:
        guardado = db.scalar(select(TokenCuenta))
    assert guardado.token_hash == security.hash_secret(token, SETTINGS.token_hmac_key)


def test_cuenta_sin_activar_no_puede_ingresar(app, session_factory):
    _crear_por_panel(app, session_factory)
    assert iniciar(TestClient(app), "nueva@ejemplo.test", "cualquier-cosa").status_code == 401


# ---------- Segundo factor ----------

def test_primer_ingreso_de_docente_exige_configurar_totp(app, session_factory):
    cuenta = crear_usuario(session_factory, rol="docente", sin_totp=True)
    c = TestClient(app)
    r = c.post("/ingresar", data={"identificador": cuenta.login, "password": PASSWORD, "csrf": csrf_de(c, "/ingresar")},
               follow_redirects=False)
    assert r.headers["location"] == "/ingresar/configurar-totp"
    pagina = c.get("/ingresar/configurar-totp")
    assert "<svg" in pagina.text  # código QR en línea, sin servicios externos
    secreto = re.search(r'class="secreto">([A-Z2-7]+)<', pagina.text).group(1)
    incorrecto = c.post("/ingresar/configurar-totp", data={"codigo": "000000", "csrf": csrf_de(c, "/ingresar/configurar-totp")})
    if pyotp.TOTP(secreto).now() != "000000":
        assert incorrecto.status_code == 401
    r = c.post("/ingresar/configurar-totp", data={"codigo": pyotp.TOTP(secreto).now(),
                                                   "csrf": csrf_de(c, "/ingresar/configurar-totp")}, follow_redirects=False)
    assert r.headers["location"] == "/portal"
    with session_factory() as db:
        usuario = db.get(Usuario, cuenta.id)
        assert usuario.totp_pendiente_cifrado is None
        assert crypto.descifrar(usuario.totp_secret_cifrado, SETTINGS.encryption_key) == secreto
    # El secreto se guarda cifrado, nunca en claro
    assert secreto not in usuario.totp_secret_cifrado


def test_representante_puede_activar_totp_opcional_y_desactivarlo(app, session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    c = entrar_web(app, cuenta)
    secreto = re.search(r'class="secreto">([A-Z2-7]+)<', c.get("/portal/perfil").text).group(1)
    post_web(c, "/portal/perfil/totp", {"codigo": pyotp.TOTP(secreto).now()})
    assert iniciar(TestClient(app), cuenta.email).json() == {"paso": "totp"}
    post_web(c, "/portal/perfil/totp/desactivar", {"actual": PASSWORD})
    assert iniciar(TestClient(app), cuenta.email).json()["paso"] == "completo"


def test_rol_con_totp_obligatorio_no_puede_desactivarlo(app, session_factory):
    cuenta = crear_usuario(session_factory, rol="docente")
    c = entrar_web(app, cuenta)
    post_web(c, "/portal/perfil/totp/desactivar", {"actual": PASSWORD})
    with session_factory() as db:
        assert db.get(Usuario, cuenta.id).totp_secret_cifrado is not None


# ---------- Recuperación ----------

def test_recuperacion_responde_igual_exista_o_no_la_cuenta(app, session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    c = TestClient(app)
    existente = c.post("/cuenta/recuperar", data={"email": cuenta.email, "csrf": csrf_de(c, "/cuenta/recuperar")})
    inexistente = c.post("/cuenta/recuperar", data={"email": "nadie@ejemplo.test", "csrf": csrf_de(c, "/cuenta/recuperar")})
    assert existente.status_code == inexistente.status_code == 200
    assert existente.text == inexistente.text
    with session_factory() as db:
        envios = db.scalars(select(EnvioSaliente)).all()
    assert len(envios) == 1 and envios[0].destino == cuenta.email


def test_recuperacion_completa_y_cierre_de_sesiones(app, session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    sesion_vieja = entrar_web(app, cuenta)
    c = TestClient(app)
    c.post("/cuenta/recuperar", data={"email": cuenta.email, "csrf": csrf_de(c, "/cuenta/recuperar")})
    with session_factory() as db:
        cuerpo = db.scalar(select(EnvioSaliente)).cuerpo
    ruta = _ruta(re.search(r"http\S+/cuenta/restablecer/\S+", cuerpo).group(0))
    r = c.post(ruta, data={"password": "Nueva frase segura", "confirmacion": "Nueva frase segura", "csrf": csrf_de(c, ruta)})
    assert "Contraseña guardada" in r.text
    assert sesion_vieja.get("/portal", follow_redirects=False).status_code == 303
    assert iniciar(TestClient(app), cuenta.email, "Nueva frase segura").json()["paso"] == "completo"
    assert iniciar(TestClient(app), cuenta.email, PASSWORD).status_code == 401


def test_formularios_publicos_exigen_token_csrf(app, session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    c = TestClient(app)
    c.get("/ingresar")
    r = c.post("/ingresar", data={"identificador": cuenta.login, "password": PASSWORD})
    assert r.status_code == 403
