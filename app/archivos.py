"""Archivos de las entregas: tipo real por contenido, límites, nombres aleatorios y almacenamiento.

- El tipo se decide por los primeros bytes (firma), no por la extensión; además ambos deben coincidir.
- Cada archivo se guarda con una clave aleatoria; el nombre original solo se usa para mostrarlo.
- Las imágenes se vuelven a codificar sin metadatos (EXIF, incluida la ubicación GPS).
- El código nunca se ejecuta: se descarga o se muestra como texto.
- Almacenamiento: disco local (desarrollo) o un bucket compatible con S3 (Neon Object Storage,
  Cloudflare R2, AWS S3) si se define ARCHIVOS_BUCKET; ver `crear_almacen`.
- Pendiente para producción: escaneo antivirus (ClamAV).
"""

import hashlib
import io
import re
import secrets
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

MB = 1024 * 1024
Image.MAX_IMAGE_PIXELS = 40_000_000  # evita imágenes descomunales que agoten la memoria

TIPOS = {
    "documento": {"extensiones": {".pdf", ".docx"}, "maximo": 10 * MB, "etiqueta": "Documento (PDF, Word)"},
    "imagen": {"extensiones": {".jpg", ".jpeg", ".png", ".webp"}, "maximo": 8 * MB, "etiqueta": "Imagen (JPG, PNG, WEBP)"},
    "video": {"extensiones": {".mp4", ".mov"}, "maximo": 50 * MB, "etiqueta": "Video (MP4, MOV)"},
    "modelo3d": {"extensiones": {".stl", ".obj", ".glb"}, "maximo": 20 * MB, "etiqueta": "Modelo 3D (STL, OBJ, GLB)"},
    "codigo": {"extensiones": {".py", ".js", ".sql", ".txt", ".zip"}, "maximo": 2 * MB, "etiqueta": "Código o texto (PY, JS, SQL, TXT, ZIP)"},
}
MAX_ARCHIVOS_POR_ENTREGA = 5
MAX_ZIP_DESCOMPRIMIDO = 50 * MB
MAX_ZIP_ARCHIVOS = 500


class ArchivoRechazado(Exception):
    def __init__(self, mensaje: str):
        super().__init__(mensaje)
        self.mensaje = mensaje


@dataclass
class ArchivoValidado:
    nombre: str
    tipo: str
    mime: str
    contenido: bytes
    sha256: str


def nombre_seguro(nombre: str) -> str:
    """Quita rutas y caracteres extraños; solo se usa para mostrar el nombre."""
    base = Path(nombre.replace("\\", "/")).name
    base = unicodedata.normalize("NFC", base)
    base = re.sub(r"[^\w.\- áéíóúÁÉÍÓÚñÑ]", "_", base).strip(" .") or "archivo"
    return base[-120:]


def _es_texto(contenido: bytes) -> bool:
    if b"\x00" in contenido:
        return False
    try:
        contenido.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _zip_valido(contenido: bytes) -> zipfile.ZipFile:
    try:
        archivo = zipfile.ZipFile(io.BytesIO(contenido))
    except zipfile.BadZipFile:
        raise ArchivoRechazado("El archivo comprimido está dañado.") from None
    infos = archivo.infolist()
    if len(infos) > MAX_ZIP_ARCHIVOS or sum(i.file_size for i in infos) > MAX_ZIP_DESCOMPRIMIDO:
        raise ArchivoRechazado("El archivo comprimido es demasiado grande al descomprimirse.")
    return archivo


def _detectar(contenido: bytes, extension: str) -> tuple[str, str] | None:
    """Devuelve (tipo, mime) según la firma del contenido, o None si no coincide con la extensión."""
    inicio = contenido[:16]
    if extension == ".pdf" and inicio.startswith(b"%PDF-"):
        return "documento", "application/pdf"
    if extension == ".docx" and inicio.startswith(b"PK\x03\x04"):
        if "word/document.xml" in _zip_valido(contenido).namelist():
            return "documento", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return None
    if extension in (".jpg", ".jpeg") and inicio.startswith(b"\xff\xd8\xff"):
        return "imagen", "image/jpeg"
    if extension == ".png" and inicio.startswith(b"\x89PNG\r\n\x1a\n"):
        return "imagen", "image/png"
    if extension == ".webp" and inicio[:4] == b"RIFF" and inicio[8:12] == b"WEBP":
        return "imagen", "image/webp"
    if extension in (".mp4", ".mov") and inicio[4:8] == b"ftyp":
        return "video", "video/mp4" if extension == ".mp4" else "video/quicktime"
    if extension == ".glb" and inicio.startswith(b"glTF"):
        return "modelo3d", "model/gltf-binary"
    if extension == ".stl":
        binario = len(contenido) >= 84 and len(contenido) == 84 + 50 * int.from_bytes(contenido[80:84], "little")
        if binario or (contenido.lstrip().startswith(b"solid") and _es_texto(contenido)):
            return "modelo3d", "model/stl"
        return None
    if extension == ".obj" and _es_texto(contenido):
        lineas = [linea.split(maxsplit=1)[0] for linea in contenido.decode().splitlines() if linea.strip()]
        if lineas and all(p in {"v", "vt", "vn", "vp", "f", "l", "o", "g", "s", "#", "mtllib", "usemtl"}
                          or p.startswith("#") for p in lineas):
            return "modelo3d", "model/obj"
        return None
    if extension == ".zip" and inicio.startswith(b"PK\x03\x04"):
        _zip_valido(contenido)
        return "codigo", "application/zip"
    if extension in (".py", ".js", ".sql", ".txt") and _es_texto(contenido):
        return "codigo", "text/plain; charset=utf-8"
    return None


def _limpiar_imagen(contenido: bytes, mime: str) -> bytes:
    """Vuelve a codificar la imagen sin metadatos. También detecta imágenes dañadas."""
    formato = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}[mime]
    try:
        with Image.open(io.BytesIO(contenido)) as original:
            original.load()
            # Primero se aplica la orientación del EXIF, para que la foto no quede girada al quitarlo
            imagen = ImageOps.exif_transpose(original)
            if imagen.mode not in ("RGB", "RGBA", "L"):
                imagen = imagen.convert("RGBA" if formato != "JPEG" else "RGB")
            if formato == "JPEG" and imagen.mode == "RGBA":
                imagen = imagen.convert("RGB")
            # Una imagen nueva con solo los píxeles: no hereda EXIF, GPS ni comentarios
            limpia = Image.new(imagen.mode, imagen.size)
            limpia.paste(imagen)
            salida = io.BytesIO()
            limpia.save(salida, format=formato, **({"quality": 90} if formato != "PNG" else {}))
            return salida.getvalue()
    except (OSError, Image.DecompressionBombError):
        raise ArchivoRechazado("La imagen está dañada o es demasiado grande.") from None


def validar(nombre: str, contenido: bytes, tipos_permitidos: list[str]) -> ArchivoValidado:
    nombre = nombre_seguro(nombre)
    extension = Path(nombre).suffix.lower()
    if not contenido:
        raise ArchivoRechazado(f"«{nombre}» está vacío.")
    detectado = _detectar(contenido, extension)
    if detectado is None:
        raise ArchivoRechazado(f"«{nombre}» no es un archivo válido de ese tipo, o la extensión no corresponde a su contenido.")
    tipo, mime = detectado
    if tipo not in tipos_permitidos:
        raise ArchivoRechazado(f"Esta tarea no admite archivos de tipo «{TIPOS[tipo]['etiqueta']}».")
    if len(contenido) > TIPOS[tipo]["maximo"]:
        raise ArchivoRechazado(f"«{nombre}» supera el máximo de {TIPOS[tipo]['maximo'] // MB} MB para su tipo.")
    if tipo == "imagen":
        contenido = _limpiar_imagen(contenido, mime)
    return ArchivoValidado(nombre=nombre, tipo=tipo, mime=mime, contenido=contenido,
                           sha256=hashlib.sha256(contenido).hexdigest())


def maximo_lectura() -> int:
    """Bytes a leer por archivo antes de rechazarlo por tamaño (el mayor de los límites + 1)."""
    return max(t["maximo"] for t in TIPOS.values()) + 1


class Almacen:
    """Almacenamiento en disco local. Para producción se reemplaza por uno compatible con S3 (R2, MinIO)
    con la misma interfaz."""

    def __init__(self, raiz: str | Path):
        self.raiz = Path(raiz)

    def _ruta(self, clave: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", clave):
            raise ValueError("Clave de archivo no válida.")
        return self.raiz / clave[:2] / clave

    @staticmethod
    def nueva_clave() -> str:
        return secrets.token_hex(16)

    def guardar(self, clave: str, contenido: bytes) -> None:
        ruta = self._ruta(clave)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(contenido)

    def leer(self, clave: str) -> bytes:
        return self._ruta(clave).read_bytes()

    def borrar(self, clave: str) -> None:
        self._ruta(clave).unlink(missing_ok=True)


class AlmacenS3:
    """Bucket compatible con S3, con la misma interfaz que `Almacen`.

    Las credenciales y el endpoint se leen de las variables estándar del SDK (AWS_ENDPOINT_URL_S3,
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION), que es como las entrega Neon.
    El bucket debe ser privado: los archivos solo salen a través de la aplicación, que verifica permisos.
    """

    def __init__(self, bucket: str, cliente=None):
        if cliente is None:
            import boto3
            from botocore.config import Config

            cliente = boto3.client("s3", config=Config(s3={"addressing_style": "path"},
                                                        retries={"max_attempts": 4, "mode": "standard"}))
        self.bucket = bucket
        self.cliente = cliente

    @staticmethod
    def _llave(clave: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", clave):
            raise ValueError("Clave de archivo no válida.")
        return f"entregas/{clave[:2]}/{clave}"

    nueva_clave = staticmethod(Almacen.nueva_clave)

    def guardar(self, clave: str, contenido: bytes) -> None:
        self.cliente.put_object(Bucket=self.bucket, Key=self._llave(clave), Body=contenido,
                                ContentType="application/octet-stream")

    def leer(self, clave: str) -> bytes:
        from botocore.exceptions import ClientError

        try:
            return self.cliente.get_object(Bucket=self.bucket, Key=self._llave(clave))["Body"].read()
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise FileNotFoundError(clave) from None
            raise

    def borrar(self, clave: str) -> None:
        self.cliente.delete_object(Bucket=self.bucket, Key=self._llave(clave))


def crear_almacen(settings) -> "Almacen | AlmacenS3":
    """Bucket S3 si ARCHIVOS_BUCKET está definido; si no, disco local."""
    if settings.archivos_bucket:
        return AlmacenS3(settings.archivos_bucket)
    return Almacen(settings.archivos_dir)
