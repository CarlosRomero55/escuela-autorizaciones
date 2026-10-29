"""Manejo de fechas: todo se guarda en UTC y se muestra en la hora de Ecuador."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TZ_ESCUELA = ZoneInfo("America/Guayaquil")


def ahora() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime) -> datetime:
    """SQLite devuelve fechas sin zona horaria; se asume UTC, que es como se guardan."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def a_utc(dt: datetime) -> datetime:
    return aware(dt).astimezone(timezone.utc)


def local(dt: datetime | None, formato: str = "%d/%m/%Y %H:%M") -> str:
    if dt is None:
        return ""
    return aware(dt).astimezone(TZ_ESCUELA).strftime(formato)


def desde_formulario(texto: str) -> datetime:
    """Convierte el valor de un campo datetime-local (hora de Ecuador, sin zona) a UTC."""
    return datetime.fromisoformat(texto).replace(tzinfo=TZ_ESCUELA).astimezone(timezone.utc)
