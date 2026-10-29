"""Manejo de fechas: todo se guarda en UTC y se muestra en la hora de Ecuador."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

TZ_ESCUELA = ZoneInfo("America/Guayaquil")


def ahora() -> datetime:
    return datetime.now(UTC)


def aware(dt: datetime) -> datetime:
    """SQLite devuelve fechas sin zona horaria; se asume UTC, que es como se guardan."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def a_utc(dt: datetime) -> datetime:
    return aware(dt).astimezone(UTC)


def local(dt: datetime | None, formato: str = "%d/%m/%Y %H:%M") -> str:
    if dt is None:
        return ""
    return aware(dt).astimezone(TZ_ESCUELA).strftime(formato)


def desde_formulario(texto: str) -> datetime:
    """Convierte el valor de un campo datetime-local (hora de Ecuador, sin zona) a UTC."""
    return datetime.fromisoformat(texto).replace(tzinfo=TZ_ESCUELA).astimezone(UTC)


def hace(dt: datetime | None) -> str:
    """Tiempo transcurrido en lenguaje sencillo: «hace 5 min», «ayer», o la fecha si es más antiguo."""
    if dt is None:
        return ""
    segundos = (ahora() - aware(dt)).total_seconds()
    if segundos < 60:
        return "hace un momento"
    if segundos < 3600:
        return f"hace {int(segundos // 60)} min"
    if segundos < 86400:
        return f"hace {int(segundos // 3600)} h"
    if segundos < 172800:
        return "ayer"
    return local(dt, "%d/%m/%Y")
