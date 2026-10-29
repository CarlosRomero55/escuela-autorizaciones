"""Sesiones en el servidor: rotación, inactividad, cierre remoto, dispositivos y alertas."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AlertaInicioSesion, DispositivoUsuario, EnvioSaliente, Sesion
from app.sesiones import COOKIE_DISPOSITIVO, COOKIE_SESION
from tests.conftest import PASSWORD, crear_usuario, csrf_de, entrar_web, post_web


def _sesiones(sf, usuario_id):
    with sf() as db:
        return db.scalars(select(Sesion).where(Sesion.usuario_id == usuario_id).order_by(Sesion.id)).all()


def test_el_token_cambia_en_cada_inicio_y_la_sesion_anterior_se_revoca(app, session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    c = entrar_web(app, cuenta)
    primer_token = c.cookies.get(COOKIE_SESION)
    c.post("/ingresar", data={"identificador": cuenta.login, "password": PASSWORD, "csrf": csrf_de(c, "/ingresar")})
    assert c.cookies.get(COOKIE_SESION) != primer_token
    primera, segunda = _sesiones(session_factory, cuenta.id)
    assert primera.revocada_en is not None and segunda.revocada_en is None


def test_el_segundo_factor_emite_un_token_nuevo(app, session_factory):
    cuenta = crear_usuario(session_factory, rol="docente")
    entrar_web(app, cuenta)
    pendiente, completa = _sesiones(session_factory, cuenta.id)
    assert pendiente.estado == "pendiente_totp" and pendiente.revocada_en is not None
    assert completa.estado == "completa" and completa.token_hash != pendiente.token_hash


def test_sesion_pendiente_de_totp_no_entra_al_portal(app, session_factory):
    cuenta = crear_usuario(session_factory, rol="docente")
    c = TestClient(app)
    c.post("/ingresar", data={"identificador": cuenta.login, "password": PASSWORD, "csrf": csrf_de(c, "/ingresar")})
    r = c.get("/portal", follow_redirects=False)
    assert r.headers["location"] == "/ingresar/totp"


def test_la_sesion_expira_por_inactividad(app, session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    c = entrar_web(app, cuenta)
    with session_factory() as db:
        sesion = db.scalars(select(Sesion).where(Sesion.usuario_id == cuenta.id)).one()
        sesion.ultima_actividad = datetime.now(UTC) - timedelta(minutes=31)
        db.commit()
    assert c.get("/portal", follow_redirects=False).headers["location"] == "/ingresar"


def test_la_sesion_expira_al_cumplir_su_duracion_maxima(app, session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    c = entrar_web(app, cuenta)
    with session_factory() as db:
        sesion = db.scalars(select(Sesion).where(Sesion.usuario_id == cuenta.id)).one()
        sesion.expira_en = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    assert c.get("/portal", follow_redirects=False).status_code == 303


def test_cerrar_sesion_en_otro_dispositivo(app, session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    computadora = entrar_web(app, cuenta)
    telefono = entrar_web(app, cuenta)
    sesion_telefono = _sesiones(session_factory, cuenta.id)[-1]
    perfil = computadora.get("/portal/perfil").text
    assert perfil.count("/cerrar\"") >= 2  # ve ambas sesiones
    post_web(computadora, f"/portal/perfil/sesiones/{sesion_telefono.id}/cerrar")
    assert telefono.get("/portal", follow_redirects=False).status_code == 303
    assert computadora.get("/portal").status_code == 200


def test_no_se_puede_cerrar_la_sesion_de_otra_persona(app, session_factory):
    victima = crear_usuario(session_factory, rol="representante")
    entrar_web(app, victima)
    sesion_victima = _sesiones(session_factory, victima.id)[-1]
    atacante = entrar_web(app, crear_usuario(session_factory, rol="representante"))
    assert post_web(atacante, f"/portal/perfil/sesiones/{sesion_victima.id}/cerrar").status_code == 404
    assert _sesiones(session_factory, victima.id)[-1].revocada_en is None


def test_cerrar_las_demas_sesiones(app, session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    otros = [entrar_web(app, cuenta) for _ in range(2)]
    actual = entrar_web(app, cuenta)
    post_web(actual, "/portal/perfil/sesiones/cerrar-otras")
    assert all(o.get("/portal", follow_redirects=False).status_code == 303 for o in otros)
    assert actual.get("/portal").status_code == 200


def test_cambiar_contraseña_cierra_las_demas_sesiones(app, session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    otra = entrar_web(app, cuenta)
    actual = entrar_web(app, cuenta)
    nueva = "Otra-frase-larga-2026"
    post_web(actual, "/portal/perfil/password", {"actual": PASSWORD, "nueva": nueva, "confirmacion": nueva})
    assert otra.get("/portal", follow_redirects=False).status_code == 303
    assert actual.get("/portal").status_code == 200


# ---------- Dispositivos y alertas ----------

def test_primer_dispositivo_no_genera_alerta(app, session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    c = entrar_web(app, cuenta)
    assert c.cookies.get(COOKIE_DISPOSITIVO)
    with session_factory() as db:
        assert db.scalar(select(AlertaInicioSesion)) is None
        assert len(db.scalars(select(DispositivoUsuario)).all()) == 1


def test_mismo_dispositivo_no_genera_alerta(app, session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    c = entrar_web(app, cuenta)
    c.post("/ingresar", data={"identificador": cuenta.login, "password": PASSWORD, "csrf": csrf_de(c, "/ingresar")})
    with session_factory() as db:
        assert db.scalar(select(AlertaInicioSesion)) is None


def test_dispositivo_nuevo_genera_alerta_y_correo(app, session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    entrar_web(app, cuenta)
    otro_navegador = entrar_web(app, cuenta)  # cliente nuevo = cookie de dispositivo nueva
    with session_factory() as db:
        alerta = db.scalar(select(AlertaInicioSesion))
        correo = db.scalar(select(EnvioSaliente))
    assert alerta is not None and alerta.estado == "pendiente"
    assert correo.destino == cuenta.email and "inicio de sesión" in correo.asunto.lower()
    assert "Fui yo" in otro_navegador.get("/portal/perfil").text


def test_no_fui_yo_cierra_la_sesion_sospechosa(app, session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    duenio = entrar_web(app, cuenta)
    intruso = entrar_web(app, cuenta)
    with session_factory() as db:
        alerta = db.scalar(select(AlertaInicioSesion))
    post_web(duenio, f"/portal/perfil/alertas/{alerta.id}", {"accion": "bloquear"})
    assert intruso.get("/portal", follow_redirects=False).status_code == 303
    assert duenio.get("/portal").status_code == 200
    with session_factory() as db:
        assert db.get(AlertaInicioSesion, alerta.id).estado == "bloqueada"
        sospechosa = db.get(Sesion, alerta.sesion_id)
        assert db.get(DispositivoUsuario, sospechosa.dispositivo_id).confiable is False


def test_la_alerta_aparece_en_la_cabecera(app, session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    entrar_web(app, cuenta)
    c = entrar_web(app, cuenta)
    assert "1 sin revisar" in c.get("/portal").text


def test_salir_revoca_la_sesion(app, session_factory):
    cuenta = crear_usuario(session_factory, rol="representante")
    c = entrar_web(app, cuenta)
    token = c.cookies.get(COOKIE_SESION)
    post_web(c, "/salir")
    assert _sesiones(session_factory, cuenta.id)[-1].revocada_en is not None
    # Aunque alguien conserve la cookie, ya no sirve
    copia = TestClient(app, cookies={COOKIE_SESION: token})
    assert copia.get("/portal", follow_redirects=False).status_code == 303
