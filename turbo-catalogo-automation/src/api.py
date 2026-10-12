"""API REST + webhook de entrada.

Endpoints
  POST /webhooks/catalogo        evento sku.created / sku.updated (firma HMAC)
  GET  /skus/{sku_id}            estado del SKU en la última corrida + cola
  GET  /hitl/pendientes          cola de aprobación humana
  POST /hitl/{sku_id}/decision   decisión del analista/líder (registro auditable)
  POST /corridas/semanal         dispara la corrida semanal (lo llama el scheduler)
  GET  /metrics                  métricas en formato Prometheus
  GET  /health                   liveness + estado de los circuit breakers

El webhook responde 202 en milisegundos y procesa en segundo plano: el
sistema de catálogo que lo llama no queda esperando al agente, y si no
recibiera respuesta rápida reintentaría y duplicaría eventos. Los
duplicados igual se ignoran por event_id (idempotencia).

Correr:  uvicorn src.api:app --reload
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from . import pipeline
from .config import REPORTS_DIR
from .datos import CB_SNOWFLAKE
from .hitl import ColaHITL
from .llm import CB_LLM
from .observabilidad import METRICAS, configurar_logs, log

app = FastAPI(title="Turbo — Control de medidas y peso del catálogo", version="1.0")
configurar_logs()
COLA = ColaHITL()
EVENTOS_VISTOS: set[str] = set()  # en producción: tabla con TTL (Redis/Postgres)


def _secreto() -> bytes:
    return os.getenv("WEBHOOK_SECRET", "dev-secret-solo-local").encode()


def firmar(cuerpo: bytes) -> str:
    return "sha256=" + hmac.new(_secreto(), cuerpo, hashlib.sha256).hexdigest()


class EventoSKU(BaseModel):
    event_id: str
    tipo: Literal["sku.created", "sku.updated"]
    sku_id: str = Field(pattern=r"^[A-Z]{3}-\d{3}$")
    campos_modificados: list[str] = []


class Decision(BaseModel):
    campo: str
    decision: Literal["aprobado", "rechazado", "pedido_mas_evidencia"]
    aprobador: str
    rol: Literal["analista_catalogo", "lider_catalogo"]
    justificacion: str = Field(min_length=5)
    valor_elegido: float | None = None


def _procesar(sku_id: str, event_id: str) -> None:
    try:
        pipeline.correr(skus=[sku_id], cola=COLA)
        log("INFO", "evento procesado", sku_id=sku_id, event_id=event_id)
    except Exception as e:  # noqa: BLE001 — el error queda en log y métricas, el webhook ya respondió
        METRICAS.inc("errores_procesamiento_evento")
        log("ERROR", "falló el procesamiento del evento", sku_id=sku_id, event_id=event_id, error=str(e))


@app.post("/webhooks/catalogo", status_code=202)
async def webhook_catalogo(request: Request, background: BackgroundTasks,
                           x_signature: str = Header(default="")):
    cuerpo = await request.body()
    if not hmac.compare_digest(firmar(cuerpo), x_signature):
        METRICAS.inc("webhook_firma_invalida")
        raise HTTPException(401, "firma inválida")
    try:
        ev = EventoSKU(**json.loads(cuerpo))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"payload inválido: {e}") from e
    if ev.event_id in EVENTOS_VISTOS:
        METRICAS.inc("webhook_duplicados")
        return {"estado": "duplicado_ignorado", "event_id": ev.event_id}
    EVENTOS_VISTOS.add(ev.event_id)
    METRICAS.inc("webhook_recibidos")
    background.add_task(_procesar, ev.sku_id, ev.event_id)
    return {"estado": "aceptado", "event_id": ev.event_id, "sku_id": ev.sku_id}


@app.get("/skus/{sku_id}")
def estado_sku(sku_id: str):
    p = REPORTS_DIR / "resultado_corrida.json"
    estado = None
    if p.exists():
        estado = json.loads(p.read_text(encoding="utf-8"))["estados"].get(sku_id)
    pend = [x for x in COLA.pendientes() if x["sku_id"] == sku_id]
    if estado is None and not pend:
        raise HTTPException(404, "SKU sin registro")
    return {"sku_id": sku_id, "estado_ultima_corrida": estado,
            "pendientes": [{"campo": x["campo"], "estado": x["estado"], "accion": x["accion"],
                            "escalado": bool(x["escalado"]), "vence": x["vence"]} for x in pend]}


@app.get("/hitl/pendientes")
def pendientes():
    return [{"sku_id": x["sku_id"], "campo": x["campo"], "estado": x["estado"], "accion": x["accion"],
             "escalado": bool(x["escalado"]), "vence": x["vence"],
             "valor_actual": x["propuesta"].get("valor_actual"),
             "valor_propuesto": x["propuesta"].get("valor_propuesto"),
             "confianza": x["propuesta"].get("confianza"), "motivo": x["propuesta"].get("motivo"),
             "evidencias": x["propuesta"].get("evidencias", [])} for x in COLA.pendientes()]


@app.post("/hitl/{sku_id}/decision")
def decidir(sku_id: str, d: Decision):
    try:
        return COLA.decidir(sku_id, d.campo, d.decision, d.aprobador, d.rol, d.justificacion, d.valor_elegido)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.post("/corridas/semanal", status_code=202)
def corrida_semanal(background: BackgroundTasks):
    background.add_task(pipeline.correr, None, None, COLA)
    return {"estado": "aceptado"}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    return METRICAS.prometheus()


@app.get("/health")
def health():
    return {"ok": True, "modo_seguro": pipeline.modo_seguro(),
            "circuitos": {"snowflake": CB_SNOWFLAKE.estado, "llm": CB_LLM.estado},
            "hitl_pendientes": len(COLA.pendientes())}
