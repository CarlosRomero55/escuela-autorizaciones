"""Limitador de intentos en memoria, por clave (por ejemplo, IP + correo).

Limitación conocida: el estado vive en un solo proceso. Con varios procesos o reinicios,
el conteo se reinicia. Para la demo de un solo contenedor es suficiente.
"""

import time
from collections import defaultdict, deque
from collections.abc import Callable


class RateLimiter:
    def __init__(self, max_hits: int, window_seconds: int, clock: Callable[[], float] = time.monotonic):
        self.max_hits = max_hits
        self.window_seconds = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        """Registra un intento y devuelve False si la clave superó el límite en la ventana."""
        now = self._clock()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window_seconds:
            hits.popleft()
        if len(hits) >= self.max_hits:
            return False
        hits.append(now)
        return True

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)
