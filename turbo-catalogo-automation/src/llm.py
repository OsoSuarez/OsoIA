"""Cliente mínimo del modelo (Anthropic) con salida JSON forzada por esquema,
reintentos, timeout y registro de costo.

Modo offline: si no hay ANTHROPIC_API_KEY, las funciones que dependen del
modelo usan su ruta alternativa determinística (ver vision.py, evals.py).
Así el repo corre completo sin credenciales y los tests son reproducibles.
"""
from __future__ import annotations

import os

from .config import MODELO_LLM
from .observabilidad import METRICAS, log
from .resiliencia import CircuitBreaker, ErrorPermanente, ErrorTransitorio, reintentar

# USD por millón de tokens (entrada, salida). Configurable: verificar el
# pricing público vigente antes de usar estos números para presupuestar.
PRECIO_MTOK = (float(os.getenv("PRECIO_IN_MTOK", "2")), float(os.getenv("PRECIO_OUT_MTOK", "10")))
CB_LLM = CircuitBreaker("llm", umbral_fallos=3, enfriamiento_s=120)


def disponible() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY")) and os.getenv("MODO_OFFLINE") != "1"


@reintentar(intentos=4, base_s=1.0, max_s=16.0, servicio="llm")
def _llamar(mensajes: list, esquema: dict, nombre: str, temperatura: float, sistema: str) -> dict:
    import anthropic

    cliente = anthropic.Anthropic(timeout=60.0, max_retries=0)  # los reintentos los manejamos acá
    try:
        r = cliente.messages.create(
            model=MODELO_LLM,
            max_tokens=1024,
            temperature=temperatura,
            system=sistema,
            messages=mensajes,
            tools=[{"name": nombre, "description": "Devuelve el resultado estructurado.", "input_schema": esquema}],
            tool_choice={"type": "tool", "name": nombre},
        )
    except (anthropic.RateLimitError, anthropic.APITimeoutError, anthropic.APIConnectionError,
            anthropic.InternalServerError) as e:
        raise ErrorTransitorio(str(e)) from e
    except anthropic.APIStatusError as e:  # 4xx: no se reintenta
        raise ErrorPermanente(str(e)) from e
    costo = (r.usage.input_tokens * PRECIO_MTOK[0] + r.usage.output_tokens * PRECIO_MTOK[1]) / 1e6
    METRICAS.sumar_costo(costo)
    METRICAS.inc("llamadas_llm")
    log("INFO", "llamada al modelo", herramienta=nombre, tokens_in=r.usage.input_tokens,
        tokens_out=r.usage.output_tokens, costo_usd=round(costo, 5))
    bloque = next(b for b in r.content if b.type == "tool_use")
    return bloque.input


def llamar_json(mensajes: list, esquema: dict, nombre: str, temperatura: float = 0.0,
                sistema: str = "Respondé solo con datos presentes en la entrada. Si no está, devolvé null.",
                verificar_phi: bool = False) -> dict:
    if verificar_phi:  # variante healthcare: nada con PHI sale hacia el proveedor del modelo
        from .salud import guardia_phi
        for m in mensajes:
            partes = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
            for parte in partes:
                if parte.get("type") == "text":
                    guardia_phi(parte["text"])
    with METRICAS.cronometro(f"llm_{nombre}"):
        return CB_LLM.llamar(_llamar, mensajes, esquema, nombre, temperatura, sistema)
