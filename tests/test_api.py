import hashlib
from datetime import UTC, datetime, timedelta

import pyotp
from sqlalchemy import select

from app.models import AdultoAutorizado, BitacoraAuditoria, Campana, Usuario
from tests.conftest import (
    CSRF,
    crear_alumno,
    crear_salon,
    crear_usuario,
    entrar_api,
    estructura,
    iniciar,
    sesion_admin,
    vincular_representante,
)

LIMITE = (datetime.now(UTC) + timedelta(days=10)).isoformat()


# ---------- Autenticación ----------

def test_login_sin_segundo_factor_para_roles_que_no_lo_exigen(client, session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    r = iniciar(client, cuenta.email)
    assert r.json() == {"paso": "completo", "rol": "representante"}
    assert client.get("/api/auth/yo").json()["rol"] == "representante"


def test_se_puede_ingresar_con_usuario_o_correo(client, session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    assert iniciar(client, cuenta.login).json()["paso"] == "completo"


def test_cookie_de_sesion_es_httponly_y_samesite_strict(client, session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    r = iniciar(client, cuenta.email)
    cookie = next(v for k, v in r.headers.multi_items() if k == "set-cookie" and v.startswith("sid="))
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie or "samesite=strict" in cookie.lower()


def test_totp_es_obligatorio_cuando_esta_configurado(client, session_factory):
    cuenta = crear_usuario(session_factory, rol="administrador")
    assert iniciar(client, cuenta.email).json() == {"paso": "totp"}
    assert client.get("/api/auth/yo").status_code == 401  # aún no hay sesión completa
    incorrecto = "000000" if pyotp.TOTP(cuenta.secreto).now() != "000000" else "111111"
    assert client.post("/api/auth/totp", json={"codigo": incorrecto}, headers=CSRF).status_code == 401
    r = client.post("/api/auth/totp", json={"codigo": pyotp.TOTP(cuenta.secreto).now()}, headers=CSRF)
    assert r.json()["paso"] == "completo"
    assert client.get("/api/auth/yo").json()["rol"] == "administrador"


def test_rol_con_totp_obligatorio_sin_configurar_debe_enrolarse(client, session_factory):
    cuenta = crear_usuario(session_factory, rol="docente", sin_totp=True)
    assert iniciar(client, cuenta.email).json() == {"paso": "enrolar_totp"}
    assert client.get("/api/auth/yo").status_code == 401


def test_mensaje_de_error_no_revela_si_la_cuenta_existe(client, session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    existente = iniciar(client, cuenta.email, password="mala-clave")
    inexistente = iniciar(client, "noexiste@ejemplo.test", password="mala-clave")
    assert existente.status_code == inexistente.status_code == 401
    assert existente.json() == inexistente.json()


def test_bloqueo_temporal_tras_cinco_fallos(client, session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    for _ in range(5):
        assert iniciar(client, cuenta.email, password="mala").status_code == 401
    # Aun con la contraseña correcta, la cuenta sigue bloqueada
    assert iniciar(client, cuenta.email).status_code == 401
    with session_factory() as db:
        usuario = db.get(Usuario, cuenta.id)
        assert usuario.bloqueado_hasta is not None
        acciones = db.scalars(select(BitacoraAuditoria.accion).where(BitacoraAuditoria.usuario_id == cuenta.id)).all()
        assert "bloqueo_temporal" in acciones and "intento_en_bloqueo" in acciones
        usuario.bloqueado_hasta = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()
    assert iniciar(client, cuenta.email).status_code == 200


def test_logout_revoca_la_sesion(client, session_factory):
    entrar_api(client, crear_usuario(session_factory, rol="representante"))
    client.post("/api/auth/logout", headers=CSRF)
    assert client.get("/api/auth/yo").status_code == 401


def test_cuenta_desactivada_pierde_acceso(client, session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    entrar_api(client, cuenta)
    with session_factory() as db:
        db.get(Usuario, cuenta.id).activo = False
        db.commit()
    assert client.get("/api/auth/yo").status_code == 401


# ---------- Protecciones generales ----------

def test_peticion_que_cambia_datos_sin_cabecera_csrf_es_rechazada(client, session_factory):
    sesion_admin(client, session_factory)
    assert client.post("/api/familias", json={"nombre_familia": "X"}).status_code == 403


def test_cabeceras_de_seguridad(client):
    r = client.get("/healthz")
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert r.headers["Cache-Control"] == "no-store"
    assert "geolocation=()" in r.headers["Permissions-Policy"]


def test_archivos_estaticos_se_sirven_desde_el_propio_dominio(client):
    assert client.get("/static/vendor/bootstrap/bootstrap.min.css").status_code == 200
    assert client.get("/static/vendor/htmx/htmx.min.js").status_code == 200
    assert client.get("/static/css/app.css").status_code == 200


def test_ruta_protegida_sin_sesion_responde_401(client):
    assert client.get("/api/alumnos").status_code == 401


# ---------- Permisos por rol en la API ----------

def test_porteria_no_puede_crear_familias(client, session_factory):
    entrar_api(client, crear_usuario(session_factory, rol="porteria"))
    assert client.post("/api/familias", json={"nombre_familia": "X"}, headers=CSRF).status_code == 403


def test_coordinacion_no_puede_crear_salones(client, session_factory):
    datos = estructura(session_factory)
    sesion_admin(client, session_factory, rol="coordinacion")
    r = client.post("/api/salones", json={"grado_id": datos["grados"]["5.º"], "paralelo_id": datos["paralelos"]["A"]},
                    headers=CSRF)
    assert r.status_code == 403


def test_administrador_crea_salon_con_nombre_derivado(client, session_factory):
    datos = estructura(session_factory)
    sesion_admin(client, session_factory)
    r = client.post("/api/salones", json={"grado_id": datos["grados"]["5.º"], "paralelo_id": datos["paralelos"]["B"]},
                    headers=CSRF)
    assert r.status_code == 201
    assert r.json()["nombre"] == "5.º B"
    duplicado = client.post("/api/salones", json={"grado_id": datos["grados"]["5.º"], "paralelo_id": datos["paralelos"]["B"]},
                            headers=CSRF)
    assert duplicado.status_code == 409


def test_listado_de_alumnos_respeta_el_alcance(client, session_factory):
    salon = crear_salon(session_factory, "5.º A")
    propio = crear_alumno(session_factory, salon, "Propio")
    crear_alumno(session_factory, salon, "Ajeno")
    rep = crear_usuario(session_factory, rol="representante")
    vincular_representante(session_factory, rep.id, propio)
    entrar_api(client, rep)
    ids = [a["id"] for a in client.get("/api/alumnos").json()]
    assert ids == [propio]


def test_alumno_nuevo_queda_matriculado(client, session_factory):
    salon = crear_salon(session_factory, "5.º A")
    sesion_admin(client, session_factory)
    r = client.post("/api/alumnos", json={"codigo_interno": "A-1", "nombres": "Ana", "apellidos": "Ficticia",
                                          "salon_id": salon}, headers=CSRF)
    assert r.status_code == 201
    assert r.json()["nombre"] == "Ana Ficticia" and r.json()["salon_id"] == salon


def test_adulto_nuevo_queda_pendiente_y_luego_se_valida(client, session_factory):
    salon = crear_salon(session_factory)
    alumno = crear_alumno(session_factory, salon)
    sesion_admin(client, session_factory, rol="coordinacion")
    adulto = client.post(f"/api/alumnos/{alumno}/adultos", json={"nombre": "Adulta Ficticia", "parentesco": "madre"},
                         headers=CSRF).json()
    assert adulto["estado"] == "pendiente"
    r = client.patch(f"/api/adultos/{adulto['id']}/estado", json={"estado": "validado"}, headers=CSRF)
    assert r.json()["estado"] == "validado"
    with session_factory() as db:
        assert db.get(AdultoAutorizado, adulto["id"]).validado_por is not None


def test_campana_guarda_hash_del_texto_y_registra_bitacora_con_rol(client, session_factory):
    salon = crear_salon(session_factory)
    sesion_admin(client, session_factory, rol="coordinacion")
    texto = "Autorizo la salida pedagógica de mi representado."
    r = client.post("/api/campanas", json={"titulo": "Salida", "tipo": "salida", "texto_legal": texto,
                                           "fecha_limite": LIMITE, "salon_ids": [salon]}, headers=CSRF)
    assert r.status_code == 201
    with session_factory() as db:
        assert db.scalar(select(Campana)).texto_hash == hashlib.sha256(texto.encode()).hexdigest()
        entrada = db.scalar(select(BitacoraAuditoria).where(BitacoraAuditoria.entidad == "campana"))
        assert entrada.accion == "crear" and entrada.rol == "coordinacion"


def test_campana_con_salon_inexistente_no_se_crea(client, session_factory):
    estructura(session_factory)
    sesion_admin(client, session_factory, rol="coordinacion")
    r = client.post("/api/campanas", json={"titulo": "Vacuna", "tipo": "vacuna", "texto_legal": "Texto",
                                           "fecha_limite": LIMITE, "salon_ids": [404]}, headers=CSRF)
    assert r.status_code == 404
    with session_factory() as db:
        assert db.scalar(select(Campana)) is None
