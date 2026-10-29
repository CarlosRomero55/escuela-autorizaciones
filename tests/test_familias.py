import hashlib
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import crypto, security
from app.models import AdultoAutorizado, BitacoraAuditoria, RespuestaAutorizacion, TokenAcceso
from tests.conftest import CSRF, SETTINGS, crear_alumno, crear_salon, sesion_admin

LIMITE = (datetime.now(UTC) + timedelta(days=10)).isoformat()
TEXTO = "Autorizo la salida pedagógica de mi representado."
TEXTO_HASH = hashlib.sha256(TEXTO.encode("utf-8")).hexdigest()


def _preparar(client, sf, salon_nombre="5.º A"):
    """Crea salón, alumno, familia vinculada y una campaña para ese salón."""
    salon = {"id": crear_salon(sf, salon_nombre)}
    alumno = {"id": crear_alumno(sf, salon["id"], "Alumno", salon_nombre)}
    familia = client.post("/api/familias", json={"nombre_familia": f"Familia {salon_nombre}"}, headers=CSRF).json()
    client.post(f"/api/familias/{familia['id']}/alumnos", json={"alumno_id": alumno["id"]}, headers=CSRF)
    campana = client.post("/api/campanas", json={
        "titulo": "Salida al parque",
        "tipo": "salida",
        "texto_legal": TEXTO,
        "fecha_limite": LIMITE,
        "salon_ids": [salon["id"]],
    }, headers=CSRF).json()
    return salon, alumno, familia, campana


def _enlace(client, familia_id) -> str:
    return client.post(f"/api/familias/{familia_id}/enlace", headers=CSRF).json()["url"]


def _token_de(url: str) -> str:
    return url.removeprefix("/f/")


def _formulario(alumno_id, texto_hash=TEXTO_HASH, decision="aceptada"):
    return {
        "alumno_id": str(alumno_id),
        "firmante": "Madre Ficticia",
        "parentesco": "madre",
        "decision": decision,
        "texto_hash": texto_hash,
    }


# ---------- Enlaces ----------

def test_enlace_muestra_alumnos_y_autorizaciones(app, session_factory):
    admin = sesion_admin(TestClient(app), session_factory)
    _, alumno, familia, campana = _preparar(admin, session_factory)
    url = _enlace(admin, familia["id"])

    familia_web = TestClient(app)  # sin sesión de personal
    r = familia_web.get(url)
    assert r.status_code == 200
    assert "Alumno 5.º A" in r.text
    assert "Salida al parque" in r.text
    assert "Revisar y responder" in r.text


def test_enlace_invalido_responde_404(app):
    r = TestClient(app).get("/f/token-que-no-existe")
    assert r.status_code == 404
    assert "no es válido" in r.text


def test_enlace_expirado_responde_404(app, session_factory):
    admin = sesion_admin(TestClient(app), session_factory)
    _, _, familia, _ = _preparar(admin, session_factory)
    url = _enlace(admin, familia["id"])
    with session_factory() as db:
        registro = db.scalar(select(TokenAcceso))
        registro.expira_en = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()
    assert TestClient(app).get(url).status_code == 404


def test_nuevo_enlace_invalida_el_anterior(app, session_factory):
    admin = sesion_admin(TestClient(app), session_factory)
    _, _, familia, _ = _preparar(admin, session_factory)
    primero = _enlace(admin, familia["id"])
    segundo = _enlace(admin, familia["id"])
    publico = TestClient(app)
    assert publico.get(primero).status_code == 404
    assert publico.get(segundo).status_code == 200


def test_el_token_se_guarda_como_hash(app, session_factory):
    admin = sesion_admin(TestClient(app), session_factory)
    _, _, familia, _ = _preparar(admin, session_factory)
    token = _token_de(_enlace(admin, familia["id"]))
    with session_factory() as db:
        guardado = db.scalar(select(TokenAcceso))
        assert guardado.token_hash != token
        assert guardado.token_hash == security.hash_secret(token, SETTINGS.token_hmac_key)


# ---------- Autorización ----------

def test_autorizacion_guarda_evidencia_y_bitacora(app, session_factory):
    admin = sesion_admin(TestClient(app), session_factory)
    _, alumno, familia, campana = _preparar(admin, session_factory)
    url = _enlace(admin, familia["id"])
    publico = TestClient(app)

    r = publico.post(
        f"{url}/campanas/{campana['id']}",
        data=_formulario(alumno["id"]),
        headers={"user-agent": "prueba-movil"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    with session_factory() as db:
        respuesta = db.scalar(select(RespuestaAutorizacion))
        assert respuesta.decision == "aceptada"
        assert respuesta.texto_hash == TEXTO_HASH
        assert respuesta.firmante == "Madre Ficticia"
        assert respuesta.user_agent == "prueba-movil"
        assert respuesta.ip is not None
        acciones = db.scalars(select(BitacoraAuditoria.accion).where(
            BitacoraAuditoria.entidad == "respuesta_autorizacion")).all()
        assert acciones == ["autorizar"]


def test_texto_alterado_se_rechaza(app, session_factory):
    admin = sesion_admin(TestClient(app), session_factory)
    _, alumno, familia, campana = _preparar(admin, session_factory)
    url = _enlace(admin, familia["id"])
    r = TestClient(app).post(
        f"{url}/campanas/{campana['id']}",
        data=_formulario(alumno["id"], texto_hash="0" * 64),
        follow_redirects=False,
    )
    assert r.status_code == 409
    with session_factory() as db:
        assert db.scalar(select(RespuestaAutorizacion)) is None


def test_respuesta_duplicada_se_rechaza(app, session_factory):
    admin = sesion_admin(TestClient(app), session_factory)
    _, alumno, familia, campana = _preparar(admin, session_factory)
    url = _enlace(admin, familia["id"])
    publico = TestClient(app)
    ruta = f"{url}/campanas/{campana['id']}"
    assert publico.post(ruta, data=_formulario(alumno["id"]), follow_redirects=False).status_code == 303
    segunda = publico.post(ruta, data=_formulario(alumno["id"], decision="rechazada"), follow_redirects=False)
    assert segunda.status_code == 409
    with session_factory() as db:
        assert len(db.scalars(select(RespuestaAutorizacion)).all()) == 1


def test_no_se_puede_responder_por_alumno_no_vinculado(app, session_factory):
    admin = sesion_admin(TestClient(app), session_factory)
    _, alumno_a, familia_a, campana = _preparar(admin, session_factory, "5.º A")
    _, alumno_b, _, _ = _preparar(admin, session_factory, "5.º B")  # alumno de otra familia
    url = _enlace(admin, familia_a["id"])
    r = TestClient(app).post(f"{url}/campanas/{campana['id']}", data=_formulario(alumno_b["id"]), follow_redirects=False)
    assert r.status_code == 404


def test_campana_de_otro_salon_no_aplica(app, session_factory):
    admin = sesion_admin(TestClient(app), session_factory)
    _, alumno_a, familia_a, _ = _preparar(admin, session_factory, "5.º A")
    otro_salon = {"id": crear_salon(session_factory, "3.º A")}
    campana_b = admin.post("/api/campanas", json={
        "titulo": "Vacuna", "tipo": "vacuna", "texto_legal": TEXTO,
        "fecha_limite": LIMITE, "salon_ids": [otro_salon["id"]],
    }, headers=CSRF).json()
    url = _enlace(admin, familia_a["id"])
    r = TestClient(app).post(f"{url}/campanas/{campana_b['id']}", data=_formulario(alumno_a["id"]), follow_redirects=False)
    assert r.status_code == 404


def test_comprobante_solo_visible_para_la_familia_dueña(app, session_factory):
    admin = sesion_admin(TestClient(app), session_factory)
    _, alumno, familia, campana = _preparar(admin, session_factory, "5.º A")
    _, _, familia_b, _ = _preparar(admin, session_factory, "5.º B")
    url_a = _enlace(admin, familia["id"])
    url_b = _enlace(admin, familia_b["id"])
    publico = TestClient(app)
    r = publico.post(f"{url_a}/campanas/{campana['id']}", data=_formulario(alumno["id"]), follow_redirects=False)
    comprobante = r.headers["location"]
    assert publico.get(comprobante).status_code == 200
    assert publico.get(comprobante.replace(_token_de(url_a), _token_de(url_b))).status_code == 404


# ---------- Adultos autorizados ----------

def test_documento_se_guarda_cifrado_y_con_hash(app, session_factory):
    admin = sesion_admin(TestClient(app), session_factory)
    _, alumno, familia, _ = _preparar(admin, session_factory)
    url = _enlace(admin, familia["id"])
    r = TestClient(app).post(f"{url}/adultos", data={
        "alumno_id": str(alumno["id"]),
        "nombre": "Adulta Ficticia",
        "parentesco": "tía",
        "telefono": "0900000000",
        "documento": "12345678",
    }, follow_redirects=False)
    assert r.status_code == 303

    with session_factory() as db:
        adulto = db.scalar(select(AdultoAutorizado).where(AdultoAutorizado.nombre == "Adulta Ficticia"))
        assert adulto.estado == "pendiente"
        assert adulto.documento_cifrado is not None
        assert "12345678" not in adulto.documento_cifrado
        assert crypto.descifrar(adulto.documento_cifrado, SETTINGS.encryption_key) == "12345678"
        assert adulto.documento_hash == security.hash_secret("12345678", SETTINGS.token_hmac_key)
        bitacora = db.scalars(select(BitacoraAuditoria.cambio).where(
            BitacoraAuditoria.accion == "registrar_adulto")).all()
        assert all("12345678" not in (c or "") for c in bitacora)


def test_maximo_dos_adultos_por_alumno(app, session_factory):
    admin = sesion_admin(TestClient(app), session_factory)
    _, alumno, familia, _ = _preparar(admin, session_factory)
    url = _enlace(admin, familia["id"])
    publico = TestClient(app)
    datos = {"alumno_id": str(alumno["id"]), "parentesco": "tía", "nombre": "Adulta Ficticia"}
    assert publico.post(f"{url}/adultos", data=datos, follow_redirects=False).status_code == 303
    assert publico.post(f"{url}/adultos", data=datos, follow_redirects=False).status_code == 303
    assert publico.post(f"{url}/adultos", data=datos, follow_redirects=False).status_code == 409
