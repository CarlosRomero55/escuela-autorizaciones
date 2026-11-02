"""Fase 6: aplicación de portería, padrón para uso sin conexión y sincronización de la cola."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AdultoAutorizado, BitacoraAuditoria, Incidente, Recogida
from tests.conftest import CSRF, crear_alumno, crear_usuario, entrar_web
from tests.test_porteria import _portero, _preparar


def _iso(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).isoformat()


# ---------- Padrón ----------

def test_padron_trae_solo_lo_minimo_y_queda_en_la_bitacora(app, session_factory):
    admin, alumno, adulto, _ = _preparar(app, session_factory)
    admin.post(f"/api/alumnos/{alumno['id']}/adultos", json={"nombre": "Pendiente Ficticio", "parentesco": "tío"},
               headers=CSRF)
    portero = _portero(app, session_factory)
    r = portero.get("/api/porteria/padron")
    assert r.status_code == 200
    datos = r.json()
    assert datetime.fromisoformat(datos["vence_en"]) > datetime.fromisoformat(datos["generado_en"])
    [fila] = datos["alumnos"]
    assert fila["id"] == alumno["id"] and fila["codigo"] == "A-001" and fila["salida"] is None
    # Solo personas validadas; sin teléfono, documento ni evidencia
    assert [a["nombre"] for a in fila["adultos"]] == ["Adulta Ficticia"]
    assert set(fila["adultos"][0]) == {"id", "nombre", "parentesco", "temporal", "desde", "hasta"}
    with session_factory() as db:
        assert db.scalar(select(BitacoraAuditoria).where(BitacoraAuditoria.accion == "descargar_padron")) is not None


def test_padron_incluye_temporal_que_empieza_hoy_y_excluye_vencidos(app, session_factory):
    admin, alumno, _, _ = _preparar(app, session_factory)
    for nombre, desde, hasta in [("Más Tarde", timedelta(hours=2), timedelta(hours=6)),
                                 ("Ya Vencida", timedelta(days=-2), timedelta(days=-1)),
                                 ("Semana Próxima", timedelta(days=7), timedelta(days=8))]:
        admin.post(f"/api/alumnos/{alumno['id']}/temporales", json={
            "nombre": nombre, "parentesco": "vecina", "vigente_desde": _iso(desde), "vigente_hasta": _iso(hasta),
            "canal": "llamada", "evidencia": "Llamada de la madre", "autorizado_por": "Madre Ficticia"}, headers=CSRF)
    adultos = _portero(app, session_factory).get("/api/porteria/padron").json()["alumnos"][0]["adultos"]
    # La que empieza en dos horas viaja con su vigencia; el teléfono decide cuándo mostrarla
    assert {a["nombre"] for a in adultos} == {"Adulta Ficticia", "Más Tarde"}


def test_padron_marca_las_salidas_de_hoy(app, session_factory):
    _, alumno, adulto, _ = _preparar(app, session_factory)
    portero = _portero(app, session_factory)
    portero.post("/api/porteria/recogidas", json={"alumno_id": alumno["id"], "adulto_id": adulto["id"]}, headers=CSRF)
    assert portero.get("/api/porteria/padron").json()["alumnos"][0]["salida"] is not None
    [salida] = portero.get("/api/porteria/salidas-hoy").json()
    assert (salida["alumno"], salida["adulto"], salida["no_autorizado"]) == ("Alumno Ficticio Uno", "Adulta Ficticia", False)


def test_padron_y_salidas_solo_para_porteria(app, session_factory):
    _preparar(app, session_factory)
    for rol in ("docente", "representante", "estudiante", "auditor", "consejeria", "directivo"):
        cliente = _portero(app, session_factory, rol=rol)
        assert cliente.get("/api/porteria/padron").status_code == 403, rol
        assert cliente.get("/api/porteria/salidas-hoy").status_code == 403, rol
    assert TestClient(app).get("/api/porteria/padron").status_code == 401
    assert _portero(app, session_factory, rol="coordinacion").get("/api/porteria/padron").status_code == 200


# ---------- Sincronización sin conexión ----------

def test_registro_sin_conexion_conserva_la_hora_del_telefono(app, session_factory):
    _, alumno, adulto, _ = _preparar(app, session_factory)
    with session_factory() as db:  # autorizada desde ayer
        db.get(AdultoAutorizado, adulto["id"]).vigente_desde = datetime.now(UTC) - timedelta(days=1)
        db.commit()
    portero = _portero(app, session_factory)
    hora = datetime.now(UTC) - timedelta(minutes=40)
    cuerpo = {"alumno_id": alumno["id"], "adulto_id": adulto["id"], "sync_id": "tel-1", "ocurrida_en": hora.isoformat(),
              "diferido": True}
    r = portero.post("/api/porteria/recogidas", json=cuerpo, headers=CSRF)
    assert r.status_code == 201 and r.json()["sin_conexion"] is True and r.json()["alerta"] is None
    # El reintento de la misma cola no duplica
    assert portero.post("/api/porteria/recogidas", json=cuerpo, headers=CSRF).json()["id"] == r.json()["id"]
    with session_factory() as db:
        [recogida] = db.scalars(select(Recogida)).all()
        ocurrida = recogida.ocurrida_en.replace(tzinfo=recogida.ocurrida_en.tzinfo or UTC)
        assert abs((ocurrida - hora).total_seconds()) < 1


def test_horas_imposibles_se_rechazan(app, session_factory):
    _, alumno, adulto, _ = _preparar(app, session_factory)
    portero = _portero(app, session_factory)
    for i, delta in enumerate([timedelta(hours=1), timedelta(hours=-30)]):
        r = portero.post("/api/porteria/recogidas", json={
            "alumno_id": alumno["id"], "adulto_id": adulto["id"], "sync_id": f"tel-{i}", "ocurrida_en": _iso(delta)},
            headers=CSRF)
        assert r.status_code == 422
    with session_factory() as db:
        assert db.scalars(select(Recogida)).all() == []


def test_autorizacion_revocada_mientras_no_habia_red(app, session_factory):
    """La entrega ya ocurrió: se registra, pero queda un incidente para coordinación."""
    admin, alumno, adulto, _ = _preparar(app, session_factory)
    portero = _portero(app, session_factory)
    admin.patch(f"/api/adultos/{adulto['id']}/estado", json={"estado": "rechazado"}, headers=CSRF)

    en_linea = portero.post("/api/porteria/recogidas", json={"alumno_id": alumno["id"], "adulto_id": adulto["id"]},
                            headers=CSRF)
    assert en_linea.status_code == 409  # con red se impide

    r = portero.post("/api/porteria/recogidas", json={
        "alumno_id": alumno["id"], "adulto_id": adulto["id"], "sync_id": "tel-revocada",
        "ocurrida_en": _iso(timedelta(minutes=-10)), "diferido": True}, headers=CSRF)
    assert r.status_code == 201 and r.json()["alerta"]
    with session_factory() as db:
        incidente = db.scalar(select(Incidente))
        assert incidente.recogida_id == r.json()["id"]


def test_sin_conexion_no_permite_inventar_un_adulto_de_otro_alumno(app, session_factory):
    admin, alumno, _, _ = _preparar(app, session_factory)
    otro_alumno = crear_alumno(session_factory, alumno["salon_id"], "Otro", "Alumno")
    with session_factory() as db:
        otro = AdultoAutorizado(alumno_id=otro_alumno, nombre="Ajeno", parentesco="tío", estado="validado")
        db.add(otro)
        db.commit()
        otro_id = otro.id
    r = _portero(app, session_factory).post("/api/porteria/recogidas", json={
        "alumno_id": alumno["id"], "adulto_id": otro_id, "sync_id": "tel-x", "ocurrida_en": _iso(timedelta(minutes=-5)),
        "diferido": True},
        headers=CSRF)
    assert r.status_code == 409


def test_incidente_sin_conexion(app, session_factory):
    _, alumno, _, _ = _preparar(app, session_factory)
    r = _portero(app, session_factory).post("/api/porteria/recogidas/no-autorizada", json={
        "alumno_id": alumno["id"], "descripcion": "Un adulto no registrado pidió llevarse al alumno.",
        "sync_id": "tel-inc", "ocurrida_en": _iso(timedelta(minutes=-15)), "diferido": True}, headers=CSRF)
    assert r.status_code == 201
    with session_factory() as db:
        assert db.get(Recogida, r.json()["recogida_id"]).sin_conexion is True


def test_envio_inmediato_con_hora_del_telefono_no_cuenta_como_sin_conexion(app, session_factory):
    admin, alumno, adulto, _ = _preparar(app, session_factory)
    portero = _portero(app, session_factory)
    r = portero.post("/api/porteria/recogidas", json={
        "alumno_id": alumno["id"], "adulto_id": adulto["id"], "sync_id": "tel-ya", "ocurrida_en": _iso(timedelta(0))},
        headers=CSRF)
    assert r.status_code == 201 and r.json()["sin_conexion"] is False
    # Sin la marca de envío diferido, una autorización revocada se bloquea aunque traiga hora del teléfono
    admin.patch(f"/api/adultos/{adulto['id']}/estado", json={"estado": "rechazado"}, headers=CSRF)
    r = portero.post("/api/porteria/recogidas", json={
        "alumno_id": alumno["id"], "adulto_id": adulto["id"], "sync_id": "tel-ya-2", "ocurrida_en": _iso(timedelta(0))},
        headers=CSRF)
    assert r.status_code == 409


# ---------- Páginas de la aplicación ----------

def test_aplicacion_solo_para_porteria_y_coordinacion(app, session_factory):
    portero = entrar_web(app, crear_usuario(session_factory, rol="porteria"))
    r = portero.get("/porteria/")
    assert r.status_code == 200 and 'src="/static/porteria/porteria.js"' in r.text
    assert "No entregue al alumno" in r.text  # el protocolo viaja en la página para leerse sin red
    # El portal lleva al personal de portería directo a su aplicación
    assert portero.get("/portal", follow_redirects=False).headers["location"] == "/porteria/"
    assert entrar_web(app, crear_usuario(session_factory, rol="coordinacion")).get("/porteria/").status_code == 200
    assert entrar_web(app, crear_usuario(session_factory, rol="docente")).get("/porteria/").status_code == 403
    assert TestClient(app).get("/porteria/", follow_redirects=False).status_code == 303


def test_service_worker_y_manifiesto(client):
    sw = client.get("/porteria/sw.js")
    assert sw.status_code == 200 and sw.headers["content-type"].startswith("text/javascript")
    assert sw.headers["cache-control"] == "no-cache"
    assert "/api/" in sw.text  # la API queda fuera de la caché
    manifiesto = client.get("/porteria/manifest.webmanifest")
    assert manifiesto.headers["content-type"].startswith("application/manifest+json")
    assert manifiesto.json()["scope"] == "/porteria/"
    assert client.get("/porteria", follow_redirects=False).headers["location"] == "/porteria/"


# ---------- Atajo de la demo pública ----------

def test_atajo_a_la_pagina_de_una_familia_solo_en_modo_demo(session_factory, tmp_path):
    from app.main import create_app
    from tests.conftest import SETTINGS, crear_salon, csrf_de, sesion_admin

    def aplicacion(demo: bool):
        return create_app(session_factory=session_factory, settings=SETTINGS.model_copy(
            update={"demo_mode": demo, "archivos_dir": str(tmp_path)}))

    admin = sesion_admin(TestClient(aplicacion(False)), session_factory)
    alumno = crear_alumno(session_factory, crear_salon(session_factory, "5.º A"), "Ana", "Demo")
    familia = admin.post("/api/familias", json={"nombre_familia": "Familia Demo"}, headers=CSRF).json()
    admin.post(f"/api/familias/{familia['id']}/alumnos", json={"alumno_id": alumno}, headers=CSRF)

    for demo, esperado in ((False, 404), (True, 303)):
        visitante = TestClient(aplicacion(demo))
        r = visitante.post("/demo/familia", data={"csrf": csrf_de(visitante, "/ingresar")}, follow_redirects=False)
        assert r.status_code == esperado
    destino = r.headers["location"]
    assert destino.startswith("/f/") and "Ana Demo" in visitante.get(destino).text


def test_healthz_detecta_una_base_sin_tablas(client, session_factory):
    """Si la base se vacía o cae, Render debe enterarse por el health check."""
    from app.models import Base

    assert client.get("/healthz").json() == {"ok": True}
    with session_factory() as db:
        Base.metadata.drop_all(db.get_bind())
    r = client.get("/healthz")
    assert r.status_code == 503 and r.json()["ok"] is False
