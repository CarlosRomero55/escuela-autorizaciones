import hashlib
from datetime import datetime, timedelta, timezone

import pyotp
from sqlalchemy import select

from app import security
from app.models import AdultoAutorizado, BitacoraAuditoria, Campana, Salon
from tests.conftest import CSRF, crear_usuario, iniciar, sesion_admin

LIMITE_FECHA = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()


# ---------- Autenticación ----------

def test_login_correcto_establece_sesion(client, session_factory):
    crear_usuario(session_factory, rol="coordinadora", email="coord@ejemplo.test")
    r = iniciar(client, "coord@ejemplo.test")
    assert r.status_code == 200
    assert r.json() == {"paso": "completo", "rol": "coordinadora"}
    assert client.get("/api/auth/yo").json()["rol"] == "coordinadora"


def test_credenciales_incorrectas_no_revelan_si_el_correo_existe(client, session_factory):
    crear_usuario(session_factory, rol="coordinadora", email="coord@ejemplo.test")
    existente = iniciar(client, "coord@ejemplo.test", password="mala-clave")
    inexistente = iniciar(client, "noexiste@ejemplo.test", password="mala-clave")
    assert existente.status_code == inexistente.status_code == 401
    assert existente.json() == inexistente.json()


def test_limite_de_intentos_de_login(client, session_factory):
    crear_usuario(session_factory, rol="coordinadora", email="coord@ejemplo.test")
    for _ in range(5):
        assert iniciar(client, "coord@ejemplo.test", password="mala").status_code == 401
    assert iniciar(client, "coord@ejemplo.test", password="mala").status_code == 429


def test_totp_es_obligatorio_cuando_esta_configurado(client, session_factory):
    secreto = security.new_totp_secret()
    crear_usuario(session_factory, rol="administrador", email="admin@ejemplo.test", totp_secret=secreto)

    r = iniciar(client, "admin@ejemplo.test")
    assert r.json() == {"paso": "totp"}
    assert client.get("/api/auth/yo").status_code == 401  # aún no hay sesión completa

    codigo_incorrecto = "000000" if pyotp.TOTP(secreto).now() != "000000" else "111111"
    assert client.post("/api/auth/totp", json={"codigo": codigo_incorrecto}, headers=CSRF).status_code == 401

    r = client.post("/api/auth/totp", json={"codigo": pyotp.TOTP(secreto).now()}, headers=CSRF)
    assert r.status_code == 200
    assert client.get("/api/auth/yo").json()["rol"] == "administrador"


def test_logout_cierra_la_sesion(client, session_factory):
    sesion_admin(client, session_factory)
    client.post("/api/auth/logout", headers=CSRF)
    assert client.get("/api/auth/yo").status_code == 401


# ---------- Protecciones generales ----------

def test_peticion_que_cambia_datos_sin_cabecera_csrf_es_rechazada(client, session_factory):
    sesion_admin(client, session_factory)
    r = client.post("/api/salones", json={"nombre": "1A", "grado": "1ro"})
    assert r.status_code == 403


def test_cabeceras_de_seguridad_presentes(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_ruta_protegida_sin_sesion_responde_401(client):
    assert client.get("/api/alumnos").status_code == 401


def test_rol_portería_no_puede_crear_salones(client, session_factory):
    crear_usuario(session_factory, rol="porteria", email="porteria@ejemplo.test")
    iniciar(client, "porteria@ejemplo.test")
    r = client.post("/api/salones", json={"nombre": "1A", "grado": "1ro"}, headers=CSRF)
    assert r.status_code == 403


def test_portería_puede_consultar_alumnos(client, session_factory):
    sesion_admin(client, session_factory)
    salon = client.post("/api/salones", json={"nombre": "1A", "grado": "1ro"}, headers=CSRF).json()
    client.post("/api/alumnos", json={"codigo_interno": "A-001", "nombre": "Alumno Ficticio 1", "salon_id": salon["id"]}, headers=CSRF)

    client.post("/api/auth/logout", headers=CSRF)
    crear_usuario(session_factory, rol="porteria", email="porteria@ejemplo.test")
    iniciar(client, "porteria@ejemplo.test")
    r = client.get("/api/alumnos")
    assert r.status_code == 200
    assert len(r.json()) == 1


# ---------- Catálogos y reglas de negocio ----------

def test_salon_duplicado_responde_409(client, session_factory):
    sesion_admin(client, session_factory)
    client.post("/api/salones", json={"nombre": "1A", "grado": "1ro"}, headers=CSRF)
    r = client.post("/api/salones", json={"nombre": "1A", "grado": "1ro"}, headers=CSRF)
    assert r.status_code == 409


def test_alumno_en_salon_inexistente_responde_404(client, session_factory):
    sesion_admin(client, session_factory)
    r = client.post("/api/alumnos", json={"codigo_interno": "A-9", "nombre": "X", "salon_id": 999}, headers=CSRF)
    assert r.status_code == 404


def test_adulto_nuevo_queda_pendiente_y_luego_se_valida(client, session_factory):
    sesion_admin(client, session_factory)
    salon = client.post("/api/salones", json={"nombre": "1A", "grado": "1ro"}, headers=CSRF).json()
    alumno = client.post(
        "/api/alumnos", json={"codigo_interno": "A-001", "nombre": "Alumno Ficticio 1", "salon_id": salon["id"]}, headers=CSRF
    ).json()
    adulto = client.post(
        f"/api/alumnos/{alumno['id']}/adultos",
        json={"nombre": "Adulta Ficticia", "parentesco": "madre"},
        headers=CSRF,
    ).json()
    assert adulto["estado"] == "pendiente"

    r = client.patch(f"/api/adultos/{adulto['id']}/estado", json={"estado": "validado"}, headers=CSRF)
    assert r.status_code == 200
    assert r.json()["estado"] == "validado"

    with session_factory() as db:
        registro = db.get(AdultoAutorizado, adulto["id"])
        assert registro.validado_por is not None
        acciones = db.scalars(select(BitacoraAuditoria.accion).where(BitacoraAuditoria.entidad == "adulto_autorizado")).all()
        assert "validar" in acciones


def test_campana_guarda_hash_del_texto_y_registra_bitacora(client, session_factory):
    sesion_admin(client, session_factory)
    salon = client.post("/api/salones", json={"nombre": "1A", "grado": "1ro"}, headers=CSRF).json()
    texto = "Autorizo la salida pedagógica de mi representado."
    payload = {
        "titulo": "Salida al parque",
        "descripcion": "Salida pedagógica de ejemplo",
        "tipo": "salida",
        "texto_legal": texto,
        "fecha_limite": LIMITE_FECHA,
        "salon_ids": [salon["id"]],
    }
    r = client.post("/api/campanas", json=payload, headers=CSRF)
    assert r.status_code == 201
    assert r.json()["salon_ids"] == [salon["id"]]

    with session_factory() as db:
        campana = db.scalar(select(Campana))
        assert campana.texto_hash == hashlib.sha256(texto.encode("utf-8")).hexdigest()
        acciones = db.scalars(select(BitacoraAuditoria.accion).where(BitacoraAuditoria.entidad == "campana")).all()
        assert acciones == ["crear"]


def test_campana_con_salon_inexistente_no_se_crea(client, session_factory):
    sesion_admin(client, session_factory)
    payload = {
        "titulo": "Vacuna",
        "tipo": "vacuna",
        "texto_legal": "Texto",
        "fecha_limite": LIMITE_FECHA,
        "salon_ids": [404],
    }
    assert client.post("/api/campanas", json=payload, headers=CSRF).status_code == 404
    with session_factory() as db:
        assert db.scalar(select(Campana)) is None


def test_bitacora_registra_inicio_de_sesion(client, session_factory):
    crear_usuario(session_factory, rol="coordinadora", email="coord@ejemplo.test")
    iniciar(client, "coord@ejemplo.test")
    with session_factory() as db:
        acciones = db.scalars(select(BitacoraAuditoria.accion)).all()
        assert "inicio_sesion" in acciones
