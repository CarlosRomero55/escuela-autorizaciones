"""Cifrado a nivel de aplicación (AES-256-GCM) para datos sensibles como el número de documento.

La clave viene de la variable ENCRYPTION_KEY y nunca se guarda en la base de datos.
Cada valor cifrado usa un nonce aleatorio; GCM también detecta cualquier alteración del texto cifrado.
"""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_BYTES = 12


def generar_clave() -> str:
    """Genera una clave nueva de 256 bits en base64 URL-safe, lista para ENCRYPTION_KEY."""
    return base64.urlsafe_b64encode(AESGCM.generate_key(bit_length=256)).decode("ascii")


def _clave_bytes(clave_b64: str) -> bytes:
    if not clave_b64:
        raise RuntimeError("ENCRYPTION_KEY no está configurada.")
    clave = base64.urlsafe_b64decode(clave_b64)
    if len(clave) != 32:
        raise RuntimeError("ENCRYPTION_KEY debe ser una clave de 32 bytes en base64.")
    return clave


def cifrar(texto: str, clave_b64: str) -> str:
    nonce = os.urandom(NONCE_BYTES)
    cifrado = AESGCM(_clave_bytes(clave_b64)).encrypt(nonce, texto.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + cifrado).decode("ascii")


def descifrar(valor: str, clave_b64: str) -> str:
    crudo = base64.urlsafe_b64decode(valor)
    nonce, cifrado = crudo[:NONCE_BYTES], crudo[NONCE_BYTES:]
    return AESGCM(_clave_bytes(clave_b64)).decrypt(nonce, cifrado, None).decode("utf-8")
