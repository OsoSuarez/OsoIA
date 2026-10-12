"""Smoke tests: validan cada pieza del pipeline sin credenciales.

    pytest -q
"""
import json
from datetime import timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src import pipeline
from src.config import STATE_DIR, Umbrales
from src.datos import cargar_catalogo, estandarizar, validar_entrada
from src.detector import asignar_grupo, detectar, lecturas_nombre, validar_unidades
from src.evals import evaluar
from src.hitl import ColaHITL, ahora
from src.parsing import PARSED_DIR, parsear_fichas
from src.rag import BaseConocimiento
from src.resiliencia import CircuitBreaker, CircuitoAbierto, ErrorTransitorio, reintentar

U = Umbrales()


@pytest.fixture(scope="module")
def catalogo():
    df, _ = cargar_catalogo(usar_snowflake=False)
    return asignar_grupo(validar_entrada(estandarizar(df), U), U)


@pytest.fixture(scope="module")
def kb():
    parsear_fichas()
    k = BaseConocimiento(persistir=False, nombre="test")
    k.indexar()
    return k


# ---------- Capa 1 y detector ----------
def test_capa1_ataja_datos_imposibles(catalogo):
    malos = set(catalogo[catalogo["MOTIVO_ENTRADA"].notna()]["SKU_ID"])
    assert malos == {"GSE-001", "SNK-008"}  # 1550 kg y falta el alto


def test_estandarizacion_de_unidades_evita_falso_positivo(catalogo):
    entrana = catalogo.set_index("SKU_ID").loc["RES-004"]
    assert entrana["NET_STD"] == pytest.approx(0.5)  # 500 G == 0,5 KG
    cola, _ = detectar(catalogo, U)
    assert "RES-004" not in set(cola["SKU_ID"])


def test_validador_packs_ambiguos():
    assert lecturas_nombre("Tiramisú Dolce 6x70 g") == [420, 70]
    df = pd.DataFrame([
        {"SKU_ID": "A", "PRODUCT_NAME": "Tiramisú Dolce 6x70 g", "NET_QUANTITY": 420, "MEASUREMENT_UNIT": "G"},
        {"SKU_ID": "B", "PRODUCT_NAME": "Galletitas Queso Kesis 3x96 g", "NET_QUANTITY": 95, "MEASUREMENT_UNIT": "G"},
        {"SKU_ID": "C", "PRODUCT_NAME": "Barrita Brava 16 g", "NET_QUANTITY": 0, "MEASUREMENT_UNIT": "G"},
    ])
    assert list(validar_unidades(df, U)["SKU_ID"]) == ["C"]


# ---------- parsing + RAG ----------
def test_parsing_descarta_campos_comerciales():
    parsear_fichas()
    texto = "".join(p.read_text(encoding="utf-8") for p in PARSED_DIR.glob("*.md"))
    assert "Peso bruto unidad" in texto
    assert "Precio" not in texto and "Condiciones de pago" not in texto


def test_rag_recupera_fuentes_del_sku_correcto(kb):
    hits = kb.buscar("QUE-005 Queso Untable Ciboulette peso bruto", "QUE-005", U)
    fuentes = {h["fuente"] for h in hits}
    assert fuentes == {"ficha_proveedor", "catalogo_fabricante"}
    assert all("QUE-005" in h["texto"] for h in hits)  # grounding por identidad


# ---------- resiliencia ----------
def test_reintento_con_backoff_y_jitter():
    esperas, llamadas = [], {"n": 0}

    @reintentar(intentos=4, base_s=1, max_s=8, servicio="test", dormir=esperas.append)
    def flaky():
        llamadas["n"] += 1
        if llamadas["n"] < 3:
            raise ErrorTransitorio("503")
        return "ok"

    assert flaky() == "ok" and llamadas["n"] == 3
    assert len(esperas) == 2 and esperas[0] <= 1 and esperas[1] <= 2  # tope exponencial


def test_circuit_breaker_corta_la_dependencia_caida():
    t = {"now": 0.0}
    cb = CircuitBreaker("x", umbral_fallos=2, enfriamiento_s=30, reloj=lambda: t["now"])

    def cae():
        raise ErrorTransitorio("timeout")

    for _ in range(2):
        with pytest.raises(ErrorTransitorio):
            cb.llamar(cae)
    with pytest.raises(CircuitoAbierto):
        cb.llamar(lambda: "no se llama")
    t["now"] = 31
    assert cb.estado == "semiabierto" and cb.llamar(lambda: "ok") == "ok" and cb.estado == "cerrado"


def test_fallback_cuando_snowflake_no_responde(monkeypatch):
    monkeypatch.setenv("SIMULAR_FALLA_FUENTE", "1")
    monkeypatch.setattr("time.sleep", lambda s: None)
    df, ruta = cargar_catalogo()
    assert ruta == "csv_muestra(fallback)" and len(df) > 0


# ---------- pipeline + evals ----------
def test_pipeline_contra_golden_set():
    rep = evaluar(Umbrales(), "test")
    assert rep["exactitud_estado"] >= 0.95
    assert rep["detector_recall"] == 1.0  # ningún error real se escapa
    assert rep["fidelidad_fuente"] == 1.0  # todo valor propuesto está en su fuente


def test_decision_de_alto_impacto_nunca_se_ejecuta_sola(tmp_path, kb):
    cola = ColaHITL(tmp_path / "h.db", silenciosa=True)
    s = pipeline.correr(cola=cola, kb=kb, guardar_reportes=False)
    bloqueos = [r for r in s["resultados"] if r["accion_propuesta"] == "bloquear_pendiente_aprobacion"]
    assert bloqueos and all(r["estado"] == "bloqueado_pendiente_aprobacion" for r in bloqueos)
    pend = {(p["sku_id"], p["campo"]): p for p in cola.pendientes()}
    assert all(pend[(r["sku_id"], r["campo"])]["vence"] is None for r in bloqueos)  # no expira solo


# ---------- HITL ----------
def test_hitl_exige_justificacion_y_eleccion_en_contradiccion(tmp_path, kb):
    cola = ColaHITL(tmp_path / "h.db", silenciosa=True)
    pipeline.correr(cola=cola, kb=kb, guardar_reportes=False)
    with pytest.raises(ValueError):
        cola.decidir("YOG-004", "peso_kg", "aprobado", "x", "analista_catalogo", "")
    with pytest.raises(ValueError):
        cola.decidir("QUE-005", "peso_kg", "aprobado", "x", "analista_catalogo", "ok, va la ficha")
    with pytest.raises(PermissionError):
        cola.decidir("YOG-004", "peso_kg", "aprobado", "x", "agente", "lo apruebo yo")
    r = cola.decidir("QUE-005", "peso_kg", "aprobado", "Marina", "analista_catalogo", "rótulo = ficha", 0.33)
    assert r["valor_aplicado"] == 0.33
    reg = cola.registro()[-1]
    assert reg["aprobador"] == "Marina" and "lacteos_del_sur.md" in reg["fuente_revisada"]  # cita trazable


def test_politica_de_espera_escala_correcciones(tmp_path, kb):
    cola = ColaHITL(tmp_path / "h.db", silenciosa=True)
    pipeline.correr(cola=cola, kb=kb, guardar_reportes=False)
    assert cola.revisar_vencimientos(ahora() + timedelta(hours=47)) == []
    esc = cola.revisar_vencimientos(ahora() + timedelta(hours=49))
    assert "YOG-004" in esc and "GSE-009" not in esc  # el bloqueo no vence


# ---------- alertas + modo seguro ----------
def test_caida_de_finalizacion_activa_modo_seguro(tmp_path, kb, monkeypatch):
    (STATE_DIR).mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "historial_kpis.json").write_text(json.dumps([{"tasa_finalizacion": 0.99}] * 4))
    alertas = pipeline.evaluar_alertas({"tasa_finalizacion": 0.80, "costo_llm_usd_por_sku_investigado": 0}, U)
    assert alertas[0]["tipo"] == "caida_finalizacion" and pipeline.modo_seguro()
    s = pipeline.correr(cola=ColaHITL(tmp_path / "h.db", silenciosa=True), kb=kb, guardar_reportes=False)
    assert not any(r["estado"] == "corregido" for r in s["resultados"])  # solo sugiere
    (STATE_DIR / "modo.json").unlink()
    (STATE_DIR / "historial_kpis.json").unlink()


# ---------- API / webhook ----------
def test_webhook_firma_idempotencia_y_endpoints():
    from src.api import app, firmar

    c = TestClient(app)
    cuerpo = json.dumps({"event_id": "ev-1", "tipo": "sku.updated", "sku_id": "GAL-007"}).encode()
    assert c.post("/webhooks/catalogo", content=cuerpo, headers={"X-Signature": "sha256=mala"}).status_code == 401
    r = c.post("/webhooks/catalogo", content=cuerpo, headers={"X-Signature": firmar(cuerpo)})
    assert r.status_code == 202 and r.json()["estado"] == "aceptado"
    r2 = c.post("/webhooks/catalogo", content=cuerpo, headers={"X-Signature": firmar(cuerpo)})
    assert r2.json()["estado"] == "duplicado_ignorado"
    assert c.get("/health").json()["ok"] is True
    assert "turbo_" in c.get("/metrics").text
    assert isinstance(c.get("/hitl/pendientes").json(), list)
