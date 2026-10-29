import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import crypto, security
from app.config import Settings
from app.main import create_app
from app.models import Base, UsuarioStaff

PASSWORD = "Clave-segura-123"
CSRF = {"X-Requested-With": "fetch"}

SETTINGS = Settings(
    database_url="sqlite://",
    secret_key="clave-de-sesion-solo-para-pruebas-123456",
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


def crear_usuario(session_factory, *, rol, email, totp_secret=None) -> int:
    with session_factory() as db:
        usuario = UsuarioStaff(
            nombre="Usuario de prueba",
            email=email,
            rol=rol,
            password_hash=security.hash_password(PASSWORD),
            totp_secret=totp_secret,
        )
        db.add(usuario)
        db.commit()
        return usuario.id


def iniciar(client, email, password=PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password}, headers=CSRF)


def sesion_admin(client, session_factory, email="admin@ejemplo.test"):
    crear_usuario(session_factory, rol="administrador", email=email)
    r = iniciar(client, email)
    assert r.status_code == 200
    return client
