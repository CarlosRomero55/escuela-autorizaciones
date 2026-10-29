"""Fixtures y ayudantes de prueba: base SQLite en memoria, estructura académica mínima y sesiones."""

import re
import uuid
from dataclasses import dataclass
from datetime import date

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import crypto, security
from app.config import Settings
from app.main import create_app
from app.models import (
    Alumno,
    AnioLectivo,
    Area,
    Base,
    Grado,
    Materia,
    Matricula,
    Nivel,
    Paralelo,
    Persona,
    RepresentanteAlumno,
    Salon,
    SalonMateriaDocente,
    Usuario,
    UsuarioRol,
)
from app.permisos import ROLES_TOTP_OBLIGATORIO
from app.tiempo import ahora

PASSWORD = "Clave-segura-123"
CSRF = {"X-Requested-With": "fetch"}
CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')

SETTINGS = Settings(
    database_url="sqlite://",
    token_hmac_key="clave-hmac-solo-para-pruebas-123456",
    encryption_key=crypto.generar_clave(),
    app_env="development",
)


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture()
def app(session_factory):
    # Una aplicación nueva por prueba: los límites de intento empiezan en cero
    return create_app(session_factory=session_factory, settings=SETTINGS)


@pytest.fixture()
def client(app):
    return TestClient(app)


# ---------- Estructura académica ----------

def estructura(sf) -> dict:
    """Año activo, grados 1.º/3.º/5.º, paralelos A/B y dos áreas con su materia. Idempotente."""
    with sf() as db:
        anio = db.scalar(select(AnioLectivo).where(AnioLectivo.activo.is_(True)))
        if anio is None:
            anio = AnioLectivo(nombre="2026-2027", inicio=date(2026, 5, 4), fin=date(2027, 2, 5), activo=True)
            db.add(anio)
            nivel = Nivel(nombre="Básica", orden=1)
            db.add(nivel)
            db.flush()
            for nombre, orden in (("1.º", 1), ("3.º", 3), ("5.º", 5)):
                db.add(Grado(nivel_id=nivel.id, nombre=nombre, orden=orden))
            db.add_all([Paralelo(nombre="A"), Paralelo(nombre="B")])
            for nombre in ("Matemática", "Lengua y Literatura"):
                area = Area(nombre=nombre)
                db.add(area)
                db.flush()
                db.add(Materia(nombre=nombre, area_id=area.id))
            db.commit()
        return {
            "anio": anio.id,
            "areas": {a.nombre: a.id for a in db.scalars(select(Area))},
            "materias": {m.nombre: m.id for m in db.scalars(select(Materia))},
            "grados": {g.nombre: g.id for g in db.scalars(select(Grado))},
            "paralelos": {p.nombre: p.id for p in db.scalars(select(Paralelo))},
        }


def crear_salon(sf, nombre: str = "5.º A", tutor_id: int | None = None) -> int:
    datos = estructura(sf)
    grado, paralelo = nombre.split(" ")
    with sf() as db:
        salon = Salon(anio_lectivo_id=datos["anio"], grado_id=datos["grados"][grado],
                      paralelo_id=datos["paralelos"][paralelo], nombre=nombre, tutor_id=tutor_id)
        db.add(salon)
        db.commit()
        return salon.id


def asignar(sf, salon_id: int, materia: str, docente_id: int) -> None:
    datos = estructura(sf)
    with sf() as db:
        db.add(SalonMateriaDocente(salon_id=salon_id, materia_id=datos["materias"][materia], docente_id=docente_id))
        db.commit()


def crear_alumno(sf, salon_id: int, nombres: str = "Alumno", apellidos: str = "Ficticio", codigo: str | None = None) -> int:
    datos = estructura(sf)
    with sf() as db:
        persona = Persona(nombres=nombres, apellidos=apellidos)
        db.add(persona)
        db.flush()
        alumno = Alumno(persona_id=persona.id, codigo_interno=codigo or f"E-{uuid.uuid4().hex[:6]}", salon_id=salon_id)
        db.add(alumno)
        db.flush()
        db.add(Matricula(alumno_id=alumno.id, salon_id=salon_id, anio_lectivo_id=datos["anio"]))
        db.commit()
        return alumno.id


# ---------- Usuarios ----------

@dataclass
class Cuenta:
    id: int
    login: str
    email: str
    secreto: str | None


def crear_usuario(sf, *, rol: str = "coordinacion", email: str | None = None, roles: list | None = None,
                  totp_secret: str | None = None, sin_totp: bool = False, area_id: int | None = None,
                  persona_id: int | None = None, password: str | None = PASSWORD) -> Cuenta:
    """Crea una cuenta activada. Los roles que exigen TOTP lo reciben configurado salvo `sin_totp`."""
    roles = roles or [rol]
    email = email or f"{roles[0]}-{uuid.uuid4().hex[:6]}@ejemplo.test"
    login = re.sub(r"[^a-z0-9._-]", "", email.split("@")[0].lower())
    if totp_secret is None and not sin_totp and any(r in ROLES_TOTP_OBLIGATORIO for r in roles):
        totp_secret = security.new_totp_secret()
    with sf() as db:
        if persona_id is None:
            persona = Persona(nombres="Usuario", apellidos="De Prueba")
            db.add(persona)
            db.flush()
            persona_id = persona.id
        usuario = Usuario(
            persona_id=persona_id, login=login, email=email,
            password_hash=security.hash_password(password) if password else None,
            totp_secret_cifrado=crypto.cifrar(totp_secret, SETTINGS.encryption_key) if totp_secret else None,
            activado_en=ahora() if password else None,
        )
        db.add(usuario)
        db.flush()
        for r in roles:
            db.add(UsuarioRol(usuario_id=usuario.id, rol=r, area_id=area_id if r == "directivo" else None))
        db.commit()
        return Cuenta(usuario.id, login, email, totp_secret)


def vincular_representante(sf, usuario_id: int, alumno_id: int, parentesco: str = "madre") -> None:
    with sf() as db:
        db.add(RepresentanteAlumno(usuario_id=usuario_id, alumno_id=alumno_id, parentesco=parentesco,
                                   validado_en=ahora()))
        db.commit()


# ---------- Sesiones ----------

def iniciar(client, email: str, password: str = PASSWORD):
    """Inicio de sesión por la API (solo el primer paso)."""
    return client.post("/api/auth/login", json={"email": email, "password": password}, headers=CSRF)


def entrar_api(client, cuenta: Cuenta):
    r = iniciar(client, cuenta.email)
    assert r.status_code == 200, r.text
    if r.json()["paso"] == "totp":
        r = client.post("/api/auth/totp", json={"codigo": pyotp.TOTP(cuenta.secreto).now()}, headers=CSRF)
        assert r.status_code == 200, r.text
    assert r.json()["paso"] == "completo"
    return client


def sesion_admin(client, session_factory, rol: str = "administrador"):
    """Sesión de API completa (con segundo factor) para un rol de gestión."""
    return entrar_api(client, crear_usuario(session_factory, rol=rol))


def csrf_de(client, url: str) -> str:
    return CSRF_RE.search(client.get(url).text).group(1)


def entrar_web(app, cuenta: Cuenta) -> TestClient:
    """Ingreso por los formularios HTML, con segundo factor si corresponde."""
    c = TestClient(app)
    r = c.post("/ingresar", data={"identificador": cuenta.login, "password": PASSWORD, "csrf": csrf_de(c, "/ingresar")},
               follow_redirects=False)
    if r.headers.get("location") == "/ingresar/totp":
        r = c.post("/ingresar/totp", data={"codigo": pyotp.TOTP(cuenta.secreto).now(),
                                           "csrf": csrf_de(c, "/ingresar/totp")}, follow_redirects=False)
    assert r.headers.get("location") == "/portal", (r.status_code, r.headers.get("location"))
    return c


def post_web(c: TestClient, url: str, datos: dict | None = None, **kwargs):
    """Envía un formulario del portal con el token CSRF de la sesión actual."""
    return c.post(url, data={**(datos or {}), "csrf": csrf_de(c, "/portal/perfil")}, follow_redirects=False, **kwargs)
