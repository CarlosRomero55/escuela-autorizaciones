from datetime import UTC, datetime, timedelta

import pyotp

from app import security

KEY = "clave-de-prueba-para-hmac"
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


# ---------- Contraseñas ----------

def test_password_hash_verifies_correct_password():
    h = security.hash_password("Contraseña-segura-1")
    assert h != "Contraseña-segura-1"
    assert security.verify_password(h, "Contraseña-segura-1")


def test_password_hash_rejects_wrong_password():
    h = security.hash_password("Contraseña-segura-1")
    assert not security.verify_password(h, "otra")


def test_verify_password_returns_false_for_invalid_hash():
    assert not security.verify_password("no-es-un-hash", "cualquiera")


# ---------- Tokens ----------

def test_token_has_at_least_128_bits_of_entropy():
    # 16 bytes en base64 URL-safe producen 22 caracteres
    token = security.generate_token()
    assert len(token) >= 22


def test_tokens_are_unique():
    tokens = {security.generate_token() for _ in range(1000)}
    assert len(tokens) == 1000


def test_hash_secret_is_deterministic_for_same_key():
    assert security.hash_secret("abc", KEY) == security.hash_secret("abc", KEY)


def test_hash_secret_changes_with_key():
    assert security.hash_secret("abc", KEY) != security.hash_secret("abc", "otra-clave")


def test_secrets_match_accepts_original_and_rejects_other_values():
    stored = security.hash_secret("token-original", KEY)
    assert security.secrets_match(stored, "token-original", KEY)
    assert not security.secrets_match(stored, "token-falso", KEY)


# ---------- Códigos de recogida ----------

def test_pickup_code_is_six_digits():
    for _ in range(200):
        code = security.generate_pickup_code()
        assert len(code) == 6
        assert code.isdigit()


def test_pickup_code_keeps_leading_zeros(monkeypatch):
    # Forzamos el generador para comprobar que la función conserva los ceros a la izquierda
    monkeypatch.setattr(security.secrets, "randbelow", lambda n: 42)
    assert security.generate_pickup_code() == "000042"


def test_pickup_code_usable_when_fresh():
    assert security.is_pickup_code_usable(
        expires_at=NOW + timedelta(minutes=10),
        used_at=None,
        attempts=0,
        now=NOW,
        max_attempts=5,
    )


def test_pickup_code_not_usable_after_expiry():
    assert not security.is_pickup_code_usable(
        expires_at=NOW - timedelta(seconds=1),
        used_at=None,
        attempts=0,
        now=NOW,
        max_attempts=5,
    )


def test_pickup_code_not_usable_after_use():
    assert not security.is_pickup_code_usable(
        expires_at=NOW + timedelta(minutes=10),
        used_at=NOW,
        attempts=1,
        now=NOW,
        max_attempts=5,
    )


def test_pickup_code_blocked_after_max_attempts():
    assert not security.is_pickup_code_usable(
        expires_at=NOW + timedelta(minutes=10),
        used_at=None,
        attempts=5,
        now=NOW,
        max_attempts=5,
    )


def test_pickup_code_expiry_uses_ttl():
    assert security.pickup_code_expiry(NOW, 10) == NOW + timedelta(minutes=10)


# ---------- TOTP ----------

def test_totp_accepts_current_code():
    secret = security.new_totp_secret()
    current = pyotp.TOTP(secret).now()
    assert security.verify_totp(secret, current)


def test_totp_rejects_wrong_code():
    secret = security.new_totp_secret()
    current = pyotp.TOTP(secret).now()
    # Elegimos un código distinto al vigente para que la prueba sea determinista
    wrong = "000000" if current != "000000" else "111111"
    assert not security.verify_totp(secret, wrong)


def test_totp_provisioning_uri_contains_issuer():
    secret = security.new_totp_secret()
    uri = security.totp_provisioning_uri(secret, "sofia@ejemplo.test")
    assert uri.startswith("otpauth://totp/")
    assert "escuela-autorizaciones" in uri
