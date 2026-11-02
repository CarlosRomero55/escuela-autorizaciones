"""Almacenamiento de archivos en un bucket compatible con S3 (Neon Object Storage, R2, AWS), simulado con moto."""

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import select

from app.archivos import Almacen, AlmacenS3, crear_almacen
from app.main import create_app
from app.models import ArchivoEntrega
from tests.conftest import SETTINGS, entrar_web, post_web
from tests.test_clases import PDF, _tarea, escuela  # noqa: F401  (fixture)


@pytest.fixture()
def s3(monkeypatch):
    for nombre, valor in {"AWS_ACCESS_KEY_ID": "prueba", "AWS_SECRET_ACCESS_KEY": "prueba",
                          "AWS_REGION": "us-east-1"}.items():
        monkeypatch.setenv(nombre, valor)
    monkeypatch.delenv("AWS_ENDPOINT_URL_S3", raising=False)
    with mock_aws():
        cliente = boto3.client("s3", region_name="us-east-1")
        cliente.create_bucket(Bucket="escuela-archivos")
        yield cliente


def test_ida_y_vuelta_y_archivo_inexistente(s3):
    almacen = AlmacenS3("escuela-archivos")
    clave = almacen.nueva_clave()
    almacen.guardar(clave, b"contenido")
    assert almacen.leer(clave) == b"contenido"
    # Se guarda bajo un prefijo, con la clave aleatoria como nombre
    assert s3.list_objects_v2(Bucket="escuela-archivos")["Contents"][0]["Key"] == f"entregas/{clave[:2]}/{clave}"
    almacen.borrar(clave)
    with pytest.raises(FileNotFoundError):
        almacen.leer(clave)
    with pytest.raises(ValueError):
        almacen.leer("../../otro-bucket/secreto")


def test_se_elige_por_configuracion(s3):
    assert isinstance(crear_almacen(SETTINGS), Almacen)
    assert isinstance(crear_almacen(SETTINGS.model_copy(update={"archivos_bucket": "escuela-archivos"})), AlmacenS3)


def test_entrega_y_descarga_usando_el_bucket(s3, session_factory, escuela):  # noqa: F811
    app = create_app(session_factory=session_factory,
                     settings=SETTINGS.model_copy(update={"archivos_bucket": "escuela-archivos"}))
    tarea = _tarea(session_factory, escuela)
    c = entrar_web(app, escuela["estudiante"])
    assert post_web(c, f"/portal/tareas/{tarea}/entregar",
                    files=[("adjuntos", ("informe.pdf", PDF, "application/pdf"))]).status_code == 303
    with session_factory() as db:
        archivo = db.scalar(select(ArchivoEntrega))
    assert s3.list_objects_v2(Bucket="escuela-archivos")["KeyCount"] == 1
    r = c.get(f"/portal/archivos/{archivo.id}")
    assert r.status_code == 200 and r.content == PDF
    # Otro representante sigue sin poder descargarlo aunque el archivo esté en el bucket
    assert entrar_web(app, escuela["otro_representante"]).get(f"/portal/archivos/{archivo.id}").status_code == 404
