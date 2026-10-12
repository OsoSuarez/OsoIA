"""Logs estructurados (JSON por línea), trazas por corrida/SKU y métricas.

- Cada evento lleva trace_id (corrida) y sku_id: se puede reconstruir qué le
  pasó a un SKU de punta a punta con un grep.
- Las métricas se exponen en formato Prometheus en GET /metrics y se vuelcan
  a reports/metricas.json al final de cada corrida.
"""
from __future__ import annotations

import json
import logging
import statistics
import threading
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import LOGS_DIR

_CTX = threading.local()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": getattr(_CTX, "trace_id", None),
        }
        extra = getattr(record, "datos", None)
        if extra:
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configurar_logs(nivel: str = "INFO", archivo: str = "pipeline.jsonl") -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("turbo")
    if logger.handlers:
        return logger
    logger.setLevel(nivel)
    fh = logging.FileHandler(LOGS_DIR / archivo, encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(JsonFormatter())
    sh.setLevel("WARNING")  # en consola solo lo que requiere atención
    logger.addHandler(sh)
    return logger


def log(nivel: str, msg: str, **datos) -> None:
    logging.getLogger("turbo").log(getattr(logging, nivel), msg, extra={"datos": datos})


def nueva_traza() -> str:
    _CTX.trace_id = f"run-{uuid.uuid4().hex[:10]}"
    return _CTX.trace_id


class Metricas:
    """Registro en memoria, seguro entre hilos. Suficiente para una corrida
    semanal; en producción se reemplaza por un cliente Prometheus/Datadog
    sin tocar el resto del código (misma interfaz)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.contadores: dict[str, int] = defaultdict(int)
        self.latencias: dict[str, list[float]] = defaultdict(list)
        self.costo_usd: float = 0.0

    def inc(self, nombre: str, n: int = 1) -> None:
        with self._lock:
            self.contadores[nombre] += n

    def observar(self, nombre: str, segundos: float) -> None:
        with self._lock:
            self.latencias[nombre].append(segundos)

    def sumar_costo(self, usd: float) -> None:
        with self._lock:
            self.costo_usd += usd

    @contextmanager
    def cronometro(self, nombre: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.observar(nombre, time.perf_counter() - t0)

    @staticmethod
    def _pct(valores: list[float], p: float) -> float:
        if not valores:
            return 0.0
        s = sorted(valores)
        idx = min(len(s) - 1, max(0, round(p * (len(s) - 1))))
        return s[idx]

    def resumen(self) -> dict:
        with self._lock:
            lat = {
                k: {
                    "n": len(v),
                    "mediana_s": round(statistics.median(v), 4) if v else 0.0,
                    "p95_s": round(self._pct(v, 0.95), 4),
                }
                for k, v in self.latencias.items()
            }
            return {
                "contadores": dict(self.contadores),
                "latencias": lat,
                "costo_usd_total": round(self.costo_usd, 4),
            }

    def prometheus(self) -> str:
        r = self.resumen()
        lineas = []
        for k, v in sorted(r["contadores"].items()):
            lineas.append(f"turbo_{k}_total {v}")
        for k, v in sorted(r["latencias"].items()):
            lineas.append(f'turbo_latencia_seconds{{etapa="{k}",q="0.5"}} {v["mediana_s"]}')
            lineas.append(f'turbo_latencia_seconds{{etapa="{k}",q="0.95"}} {v["p95_s"]}')
        lineas.append(f"turbo_costo_usd_total {r['costo_usd_total']}")
        return "\n".join(lineas) + "\n"


METRICAS = Metricas()
