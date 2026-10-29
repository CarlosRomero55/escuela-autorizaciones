import re
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AdultoAutorizado, CodigoRecogida, Incidente, Recogida
from tests.conftest import CSRF, crear_usuario, iniciar, sesion_admin

AHORA = datetime.now(timezone.utc)
PATRON_CODIGO = re.compile(r'class="numero">(\d{6})<')


def _escuela(admin):
    """Salón, alumno, familia vinculada y una adulta validada por la coordinación."""
    salon = admin.post("/api/salones", json={"nombre": "1A", "grado": "1ro"}, headers=CSRF).json()
    alumno = admin.post("/api/alumnos", json={
        "codigo_interno": "A-001", "nombre": "Alumno Ficticio 1", "salon_id": salon["id"]}, headers=CSRF).json()
    familia = admin.post("/api/familias", json={"nombre_familia": "Familia Ficticia"}, headers=CSRF).json()
    admin.post(f"/api/familias/{familia['id']}/alumnos", json={"alumno_id": alumno["id"]}, headers=CSRF)
    url = admin.post(f"/api/familias/{familia['id']}/enlace", headers=CSRF).json()["url"]
    adulto = admin.post(f"/api/alumnos/{alumno['id']}/adultos", json={
        "nombre": "Adulta Ficticia", "parentesco": "tía", "familia_id": familia["id"]}, headers=CSRF).json()
    admin.patch(f"/api/adultos/{adulto['id']}/estado", json={"estado": "validado"}, headers=CSRF)
    return alumno, adulto, url


def _generar_codigo(app, url, alumno_id, adulto_id) -> str:
    r = TestClient(app).post(f"{url}/codigos", data={"alumno_id": alumno_id, "adulto_id": adulto_id})
    assert r.status_code == 200, r.text
    return PATRON_CODIGO.search(r.text).group(1)




def _portero(app, session_factory, rol="porteria"):
    correo = f"{rol}-{uuid.uuid4().hex[:6]}@ejemplo.test"
    crear_usuario(session_factory, rol=rol, email=correo)
    cliente = TestClient(app)
    assert iniciar(cliente, correo).status_code == 200
    return cliente


def _preparar(app, session_factory):
    admin = sesion_admin(TestClient(app), session_factory)
    alumno, adulto, url = _escuela(admin)
    return admin, alumno, adulto, url


# ---------- Códigos de recogida ----------

def test_codigo_valido_registra_recogida_una_sola_vez(app, session_factory):
    _, alumno, adulto, url = _preparar(app, session_factory)
    codigo = _generar_codigo(app, url, alumno["id"], adulto["id"])
    portero = _portero(app, session_factory)

    r = portero.post("/api/porteria/recogidas/validar-codigo", json={"alumno_id": alumno["id"], "codigo": codigo}, headers=CSRF)
    assert r.status_code == 201
    assert r.json()["adulto_id"] == adulto["id"]

    segunda = portero.post("/api/porteria/recogidas/validar-codigo", json={"alumno_id": alumno["id"], "codigo": codigo}, headers=CSRF)
    assert segunda.status_code == 404  # el código ya se usó


def test_codigo_se_guarda_solo_como_hash(app, session_factory):
    _, alumno, adulto, url = _preparar(app, session_factory)
    codigo = _generar_codigo(app, url, alumno["id"], adulto["id"])
    with session_factory() as db:
        guardado = db.scalar(select(CodigoRecogida))
        assert guardado.codigo_hash != codigo
        assert len(guardado.codigo_hash) == 64


def test_intentos_fallidos_agotan_el_codigo(app, session_factory):
    _, alumno, adulto, url = _preparar(app, session_factory)
    codigo = _generar_codigo(app, url, alumno["id"], adulto["id"])
    portero = _portero(app, session_factory)
    incorrecto = "000000" if codigo != "000000" else "111111"

    for _ in range(5):
        r = portero.post("/api/porteria/recogidas/validar-codigo", json={"alumno_id": alumno["id"], "codigo": incorrecto}, headers=CSRF)
        assert r.status_code == 422

    r = portero.post("/api/porteria/recogidas/validar-codigo", json={"alumno_id": alumno["id"], "codigo": codigo}, headers=CSRF)
    assert r.status_code == 409  # aunque sea el correcto, ya agotó los intentos


def test_codigo_expirado_no_sirve(app, session_factory):
    _, alumno, adulto, url = _preparar(app, session_factory)
    codigo = _generar_codigo(app, url, alumno["id"], adulto["id"])
    with session_factory() as db:
        registro = db.scalar(select(CodigoRecogida))
        registro.expira_en = AHORA - timedelta(minutes=1)
        db.commit()
    portero = _portero(app, session_factory)
    r = portero.post("/api/porteria/recogidas/validar-codigo", json={"alumno_id": alumno["id"], "codigo": codigo}, headers=CSRF)
    assert r.status_code == 409


def test_nuevo_codigo_invalida_el_anterior(app, session_factory):
    _, alumno, adulto, url = _preparar(app, session_factory)
    primero = _generar_codigo(app, url, alumno["id"], adulto["id"])
    segundo = _generar_codigo(app, url, alumno["id"], adulto["id"])
    portero = _portero(app, session_factory)
    if primero != segundo:
        r = portero.post("/api/porteria/recogidas/validar-codigo", json={"alumno_id": alumno["id"], "codigo": primero}, headers=CSRF)
        assert r.status_code in (422, 409)
    r = portero.post("/api/porteria/recogidas/validar-codigo", json={"alumno_id": alumno["id"], "codigo": segundo}, headers=CSRF)
    assert r.status_code == 201


def test_adulto_pendiente_no_puede_generar_codigo(app, session_factory):
    admin = sesion_admin(TestClient(app), session_factory)
    salon = admin.post("/api/salones", json={"nombre": "1A", "grado": "1ro"}, headers=CSRF).json()
    alumno = admin.post("/api/alumnos", json={"codigo_interno": "A-9", "nombre": "Alumno 9", "salon_id": salon["id"]}, headers=CSRF).json()
    familia = admin.post("/api/familias", json={"nombre_familia": "Familia 9"}, headers=CSRF).json()
    admin.post(f"/api/familias/{familia['id']}/alumnos", json={"alumno_id": alumno["id"]}, headers=CSRF)
    url = admin.post(f"/api/familias/{familia['id']}/enlace", headers=CSRF).json()["url"]
    pendiente = admin.post(f"/api/alumnos/{alumno['id']}/adultos", json={
        "nombre": "Pendiente Ficticio", "parentesco": "tío", "familia_id": familia["id"]}, headers=CSRF).json()

    r = TestClient(app).post(f"{url}/codigos", data={"alumno_id": alumno["id"], "adulto_id": pendiente["id"]})
    assert r.status_code == 409


# ---------- Consulta ----------

def test_busqueda_por_nombre_y_salon(app, session_factory):
    admin, alumno, _, _ = _preparar(app, session_factory)
    portero = _portero(app, session_factory)
    por_nombre = portero.get("/api/porteria/alumnos", params={"q": "ficticio"}).json()
    assert [a["id"] for a in por_nombre] == [alumno["id"]]
    por_salon = portero.get("/api/porteria/alumnos", params={"salon_id": alumno["salon_id"]}).json()
    assert len(por_salon) == 1
    assert portero.get("/api/porteria/alumnos", params={"q": "inexistente"}).json() == []


def test_adultos_vigentes_excluye_pendientes_y_temporales_vencidos(app, session_factory):
    admin, alumno, adulto, _ = _preparar(app, session_factory)
    admin.post(f"/api/alumnos/{alumno['id']}/temporales", json={
        "nombre": "Temporal Vencida", "parentesco": "vecina",
        "vigente_desde": (AHORA - timedelta(days=3)).isoformat(),
        "vigente_hasta": (AHORA - timedelta(days=1)).isoformat(),
        "canal": "llamada", "evidencia": "Llamada registrada por coordinación", "autorizado_por": "Madre Ficticia",
    }, headers=CSRF)
    admin.post(f"/api/alumnos/{alumno['id']}/temporales", json={
        "nombre": "Temporal Vigente", "parentesco": "vecina",
        "vigente_desde": (AHORA - timedelta(hours=1)).isoformat(),
        "vigente_hasta": (AHORA + timedelta(days=1)).isoformat(),
        "canal": "nota firmada", "evidencia": "Nota firmada por la madre", "autorizado_por": "Madre Ficticia",
    }, headers=CSRF)

    portero = _portero(app, session_factory)
    nombres = {a["nombre"] for a in portero.get(f"/api/porteria/alumnos/{alumno['id']}/adultos").json()}
    assert nombres == {"Adulta Ficticia", "Temporal Vigente"}


def test_maestra_no_puede_validar_codigos(app, session_factory):
    _, alumno, adulto, url = _preparar(app, session_factory)
    codigo = _generar_codigo(app, url, alumno["id"], adulto["id"])
    maestra = _portero(app, session_factory, rol="maestra")
    r = maestra.post("/api/porteria/recogidas/validar-codigo", json={"alumno_id": alumno["id"], "codigo": codigo}, headers=CSRF)
    assert r.status_code == 403


# ---------- Persona no autorizada y sincronización ----------

def test_persona_no_autorizada_registra_incidente_y_devuelve_protocolo(app, session_factory):
    _, alumno, _, _ = _preparar(app, session_factory)
    portero = _portero(app, session_factory)
    r = portero.post("/api/porteria/recogidas/no-autorizada", json={
        "alumno_id": alumno["id"],
        "descripcion": "Persona que no figura en la lista llegó a recoger al alumno.",
        "personas_involucradas": "Persona sin identificar",
    }, headers=CSRF)
    assert r.status_code == 201
    datos = r.json()
    assert len(datos["pasos"]) >= 4
    with session_factory() as db:
        recogida = db.get(Recogida, datos["recogida_id"])
        assert recogida.no_autorizado is True
        assert recogida.adulto_id is None
        assert db.get(Incidente, datos["incidente_id"]).recogida_id == recogida.id


def test_sync_id_repetido_no_duplica_la_recogida(app, session_factory):
    _, alumno, adulto, _ = _preparar(app, session_factory)
    portero = _portero(app, session_factory)
    datos = {"alumno_id": alumno["id"], "adulto_id": adulto["id"], "sync_id": "evento-offline-001"}
    primera = portero.post("/api/porteria/recogidas", json=datos, headers=CSRF)
    segunda = portero.post("/api/porteria/recogidas", json=datos, headers=CSRF)
    assert primera.status_code == 201
    assert segunda.status_code == 201
    assert primera.json()["id"] == segunda.json()["id"]
    with session_factory() as db:
        assert len(db.scalars(select(Recogida)).all()) == 1


def test_recogida_manual_solo_para_adulto_vigente(app, session_factory):
    admin, alumno, adulto, _ = _preparar(app, session_factory)
    portero = _portero(app, session_factory)
    otra = admin.post(f"/api/alumnos/{alumno['id']}/adultos", json={"nombre": "Pendiente X", "parentesco": "tío"}, headers=CSRF).json()
    r = portero.post("/api/porteria/recogidas", json={"alumno_id": alumno["id"], "adulto_id": otra["id"]}, headers=CSRF)
    assert r.status_code == 409
    r = portero.post("/api/porteria/recogidas", json={"alumno_id": alumno["id"], "adulto_id": adulto["id"]}, headers=CSRF)
    assert r.status_code == 201
