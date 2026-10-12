"""Fallbacks y políticas de espera.

- reintentar(): exponential backoff + jitter, solo para errores transitorios
  (timeouts, 429, 5xx). Un 4xx no se reintenta: reintentar un request mal
  armado no lo arregla.
- CircuitBreaker: si un servicio falla N veces seguidas se deja de llamarlo
  durante un tiempo y se responde con el fallback inmediato (protege al
  pipeline de quedarse colgado esperando a una dependencia caída).
- con_fallback(): encadena una ruta primaria y una alternativa, dejando en el
  log cuál se usó.
"""
from __future__ import annotations

import functools
import random
import time
from typing import Callable, TypeVar

from .observabilidad import METRICAS, log

T = TypeVar("T")


class ErrorTransitorio(Exception):
    """Timeout, 429 o 5xx: vale la pena reintentar."""


class ErrorPermanente(Exception):
    """4xx, dato inválido, credencial mala: reintentar no sirve."""


class CircuitoAbierto(Exception):
    pass


def reintentar(
    intentos: int = 4,
    base_s: float = 0.5,
    max_s: float = 8.0,
    servicio: str = "externo",
    dormir: Callable[[float], None] = time.sleep,
):
    """Decorador. Espera = random(0, min(max_s, base_s * 2**n)) — 'full jitter'."""

    def deco(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            for n in range(intentos):
                try:
                    return fn(*args, **kwargs)
                except ErrorTransitorio as e:
                    if n == intentos - 1:
                        log("ERROR", "reintentos agotados", servicio=servicio, intentos=intentos, error=str(e))
                        METRICAS.inc(f"reintentos_agotados_{servicio}")
                        raise
                    espera = random.uniform(0, min(max_s, base_s * 2**n))
                    METRICAS.inc(f"reintentos_{servicio}")
                    log("WARNING", "error transitorio, reintento", servicio=servicio, intento=n + 1,
                        espera_s=round(espera, 3), error=str(e))
                    dormir(espera)
            raise RuntimeError("inalcanzable")

        return wrapper

    return deco


class CircuitBreaker:
    def __init__(self, nombre: str, umbral_fallos: int = 3, enfriamiento_s: float = 60.0,
                 reloj: Callable[[], float] = time.monotonic):
        self.nombre = nombre
        self.umbral = umbral_fallos
        self.enfriamiento = enfriamiento_s
        self.reloj = reloj
        self.fallos = 0
        self.abierto_desde: float | None = None

    @property
    def estado(self) -> str:
        if self.abierto_desde is None:
            return "cerrado"
        if self.reloj() - self.abierto_desde >= self.enfriamiento:
            return "semiabierto"  # se deja pasar una prueba
        return "abierto"

    def llamar(self, fn: Callable[..., T], *args, **kwargs) -> T:
        if self.estado == "abierto":
            METRICAS.inc(f"circuito_abierto_{self.nombre}")
            raise CircuitoAbierto(self.nombre)
        try:
            res = fn(*args, **kwargs)
        except (ErrorTransitorio, CircuitoAbierto, TimeoutError) as e:
            self.fallos += 1
            if self.fallos >= self.umbral or self.estado == "semiabierto":
                self.abierto_desde = self.reloj()
                log("ERROR", "circuit breaker abierto", servicio=self.nombre, fallos=self.fallos)
            raise e
        self.fallos = 0
        self.abierto_desde = None
        return res


def con_fallback(primaria: Callable[[], T], alternativa: Callable[[], T], etiqueta: str) -> tuple[T, str]:
    """Devuelve (resultado, ruta_usada)."""
    try:
        return primaria(), "primaria"
    except Exception as e:  # noqa: BLE001 — se registra y se degrada
        METRICAS.inc(f"fallback_{etiqueta}")
        log("WARNING", "usando ruta alternativa", etapa=etiqueta, motivo=type(e).__name__, detalle=str(e)[:200])
        return alternativa(), "fallback"
