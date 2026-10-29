"""Reglas de seguridad: contraseñas, tokens, códigos de recogida y TOTP.

Todas las funciones son puras (no tocan base de datos ni red) para poder probarlas
de forma unitaria. El tiempo se recibe como parámetro para evitar dependencias ocultas.
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_password_hasher = PasswordHasher()


# ---------- Contraseñas del personal ----------

def hash_password(password: str) -> str:
    """Devuelve el hash argon2 de la contraseña. Nunca se guarda la contraseña en claro."""
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Compara una contraseña con su hash sin revelar detalles del fallo."""
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


PASSWORD_MIN = 10
PASSWORD_MAX = 128
# Lista corta de contraseñas muy usadas; en producción conviene una lista más amplia
_COMUNES = {
    "1234567890", "contraseña", "contrasena", "password123", "qwertyuiop", "escuela2026",
    "escuela123", "0987654321", "abcdefghij", "1111111111", "iloveyou12", "ecuador123",
}


def validar_password(password: str, *, login: str = "", email: str | None = None) -> list[str]:
    """Devuelve la lista de problemas de la contraseña; vacía si cumple la política."""
    errores = []
    if len(password) < PASSWORD_MIN:
        errores.append(f"Debe tener al menos {PASSWORD_MIN} caracteres.")
    if len(password) > PASSWORD_MAX:
        errores.append(f"Debe tener como máximo {PASSWORD_MAX} caracteres.")
    if len(set(password)) < 4:
        errores.append("Use caracteres más variados.")
    minuscula = password.lower()
    if minuscula in _COMUNES:
        errores.append("Es una contraseña demasiado común.")
    for dato in (login, (email or "").split("@")[0]):
        if dato and len(dato) >= 3 and dato.lower() in minuscula:
            errores.append("No debe contener su usuario ni su correo.")
            break
    return errores


# ---------- Tokens de familia y secretos guardados como hash ----------

def generate_token() -> str:
    """Token aleatorio de 128 bits (16 bytes), codificado en URL-safe base64."""
    return secrets.token_urlsafe(16)


def hash_secret(value: str, key: str) -> str:
    """HMAC-SHA256 del valor con una clave del servidor.

    Se usa para guardar tokens y códigos en la base de datos. Si la base se filtra,
    el atacante no puede convertir el hash en el valor original sin la clave.
    """
    return hmac.new(key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def secrets_match(expected_hash: str, candidate_value: str, key: str) -> bool:
    """Compara en tiempo constante el hash guardado con el valor recibido."""
    return hmac.compare_digest(expected_hash, hash_secret(candidate_value, key))


# ---------- Códigos de recogida ----------

def generate_pickup_code() -> str:
    """Código numérico de 6 dígitos, con ceros a la izquierda."""
    return f"{secrets.randbelow(1_000_000):06d}"


def pickup_code_expiry(now: datetime, ttl_minutes: int) -> datetime:
    return now + timedelta(minutes=ttl_minutes)


def is_pickup_code_usable(
    *,
    expires_at: datetime,
    used_at: datetime | None,
    attempts: int,
    now: datetime,
    max_attempts: int,
) -> bool:
    """Un código sirve solo si no se usó, no expiró y no superó los intentos permitidos."""
    if used_at is not None:
        return False
    if attempts >= max_attempts:
        return False
    return now < expires_at


# ---------- Segundo factor TOTP ----------

def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, account_name: str, issuer: str = "escuela-autorizaciones") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account_name, issuer_name=issuer)


def verify_totp(secret: str, code: str) -> bool:
    """Acepta el código actual y el anterior o siguiente (ventana de ±30 s)."""
    return pyotp.TOTP(secret).verify(code, valid_window=1)
