"""Multimodal: lectura del rótulo logístico fotografiado (peso bruto y
dimensiones impresas) con un modelo de visión.

Online: imagen + esquema JSON obligatorio -> el modelo no puede devolver
texto libre. Temperatura 0.
Offline / fallback: respuestas grabadas (data/fotos/lecturas_grabadas.json),
el mismo patrón que usan los tests de integración contra APIs externas.
Si no hay foto o es ilegible, la foto simplemente no suma evidencia: nunca
bloquea el flujo.

La foto es evidencia NO oficial: puede subir la confianza junto a otra
fuente, pero sola nunca habilita una corrección directa.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from . import llm
from .config import DATA_DIR, TEMPERATURA_EXTRACCION
from .datos import a_float
from .observabilidad import METRICAS, log
from .resiliencia import con_fallback

FOTOS_DIR = DATA_DIR / "fotos"

ESQUEMA_ROTULO = {
    "type": "object",
    "properties": {
        "legible": {"type": "boolean"},
        "peso_kg": {"type": ["number", "null"], "description": "Peso BRUTO impreso, convertido a kg"},
        "dimensiones_cm": {"type": ["string", "null"], "description": "L x A x H tal como figura, en cm"},
        "ref_cliente": {"type": ["string", "null"], "description": "Código de referencia impreso, si existe"},
    },
    "required": ["legible", "peso_kg", "dimensiones_cm", "ref_cliente"],
}

PROMPT = ("Esta es la foto de un rótulo logístico de un producto. Extraé el peso bruto (en kg) y las "
          "dimensiones en cm exactamente como están impresas. Si un dato no se lee con claridad, "
          "devolvé null en ese campo. No estimes ni completes datos.")


def _online(path: Path) -> dict:
    img = base64.standard_b64encode(path.read_bytes()).decode()
    return llm.llamar_json(
        [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}},
            {"type": "text", "text": PROMPT}]}],
        ESQUEMA_ROTULO, "leer_rotulo", TEMPERATURA_EXTRACCION)


def _grabada(sku_id: str) -> dict:
    grab = json.loads((FOTOS_DIR / "lecturas_grabadas.json").read_text(encoding="utf-8"))
    return grab.get(sku_id, {"legible": False, "peso_kg": None, "dimensiones_cm": None})


def leer_rotulo(sku_id: str) -> dict | None:
    path = FOTOS_DIR / f"{sku_id}.png"
    if not path.exists():
        return None
    with METRICAS.cronometro("vision_rotulo"):
        if llm.disponible():
            res, ruta = con_fallback(lambda: _online(path), lambda: _grabada(sku_id), "vision")
        else:
            res, ruta = _grabada(sku_id), "grabada"
    METRICAS.inc("fotos_leidas")
    dims = None
    if res.get("dimensiones_cm"):
        nums = re.findall(r"\d+(?:[.,]\d+)?", res["dimensiones_cm"])
        if len(nums) == 3:
            dims = [a_float(n) for n in nums]
    out = {"peso_kg": res.get("peso_kg"), "dims": dims, "legible": bool(res.get("legible")),
           "archivo": path.name, "ruta": ruta}
    log("INFO", "rótulo leído", sku_id=sku_id, ruta=ruta, legible=out["legible"], peso_kg=out["peso_kg"])
    return out
