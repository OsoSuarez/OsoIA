"""Orquestador: corrida semanal completa o corrida por evento (webhook).

Flujo:
 1. cargar catálogo (Snowflake -> fallback CSV)
 2. Capa 1: estandarizar unidades + validar entrada  -> dato_incompleto
 3. parsing de fichas (LlamaCloud -> fallback pypdf) + indexado vectorial
 4. Agente 1: rangos IQR + validador de unidades     -> cola priorizada
 5. Agente 2: RAG + foto + histórico                 -> estado + acción
 6. corregir_directo -> escritura (dry-run) + muestra de auditoría 15%
    resto            -> cola HITL con política de espera
 7. KPIs + alertas; si la tasa de finalización cae >15 pp vs. el promedio de
    las 4 corridas previas, el sistema entra en MODO SEGURO ("solo sugiere").
"""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timezone

import pandas as pd

from .agente2 import Investigador, estado_sku
from .config import REPORTS_DIR, STATE_DIR, Umbrales, cargar_umbrales, umbrales_dict
from .datos import cargar_catalogo, estandarizar, validar_entrada
from .detector import asignar_grupo, detectar
from .hitl import ESCRITOR, ColaHITL
from .observabilidad import METRICAS, configurar_logs, log, nueva_traza
from .parsing import parsear_fichas
from .rag import BaseConocimiento

MODO_PATH = STATE_DIR / "modo.json"
HIST_KPI = STATE_DIR / "historial_kpis.json"


def modo_seguro() -> bool:
    try:
        return json.loads(MODO_PATH.read_text())["modo"] == "solo_sugiere"
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return False


def fijar_modo(modo: str, motivo: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MODO_PATH.write_text(json.dumps({"modo": modo, "motivo": motivo,
                                     "desde": datetime.now(timezone.utc).isoformat()}))
    log("WARNING", "cambio de modo del Agente 2", modo=modo, motivo=motivo)


def correr(skus: list[str] | None = None, u: Umbrales | None = None, cola: ColaHITL | None = None,
           kb: BaseConocimiento | None = None, guardar_reportes: bool = True, semilla: int = 7) -> dict:
    configurar_logs()
    METRICAS.__init__()  # métricas por corrida
    traza = nueva_traza()
    u = u or cargar_umbrales()
    cola = cola or ColaHITL()
    seguro = modo_seguro()
    log("INFO", "inicio de corrida", modo="evento" if skus else "semanal", umbrales=u.version, modo_seguro=seguro)

    with METRICAS.cronometro("corrida_total"):
        df, fuente = cargar_catalogo()
        df = asignar_grupo(validar_entrada(estandarizar(df), u), u)

        if kb is None:
            parsear_fichas()
            kb = BaseConocimiento()
            kb.indexar()

        incompletos = df[df["MOTIVO_ENTRADA"].notna()]
        cola_det, rangos = detectar(df, u)  # rangos siempre sobre el catálogo completo
        if skus:
            cola_det = cola_det[cola_det["SKU_ID"].isin(skus)] if len(cola_det) else cola_det
            incompletos = incompletos[incompletos["SKU_ID"].isin(skus)]

        inv = Investigador(kb, df, rangos, u)
        resultados = []
        for _, fila in cola_det.iterrows():
            res = inv.investigar(fila)
            if seguro and res.accion_propuesta == "corregir_directo":
                res.estado, res.accion_propuesta = "pendiente_aprobacion", "corregir_y_notificar"
                res.motivo = "modo seguro activo (solo sugiere): " + res.motivo
            resultados.append(res)

        # ---- ejecución ----
        rng = random.Random(semilla)
        corregidos = [r for r in resultados if r.estado == "corregido"]
        for r in corregidos if guardar_reportes else []:  # en evals no se escribe nada
            ESCRITOR.corregir(r.sku_id, r.campo, r.valor_actual, r.valor_propuesto,
                              fuente=f"{r.fuente_principal} ({r.evidencias[0].cita})", version=r.umbrales_version)
        n_audit = math.ceil(len(corregidos) * u.fraccion_auditoria) if corregidos else 0
        auditoria = rng.sample(corregidos, n_audit)
        for r in resultados:
            if r.estado not in ("corregido", "ok"):
                cola.encolar(r.a_dict(), u)
        for _, r in incompletos.iterrows():
            cola.encolar({"sku_id": r["SKU_ID"], "campo": "*", "estado": "dato_incompleto",
                          "accion_propuesta": "ninguna", "motivo": r["MOTIVO_ENTRADA"], "valor_actual": None,
                          "desvio_vs_rango_pct": 0, "rotacion": r["ROTACION"], "evidencias": []}, u)
        vencidos = cola.revisar_vencimientos()

    # ---- estado final por SKU ----
    universo = df if not skus else df[df["SKU_ID"].isin(skus)]
    por_sku: dict[str, list] = {}
    for r in resultados:
        por_sku.setdefault(r.sku_id, []).append(r)
    estados = {}
    for _, r in universo.iterrows():
        s = r["SKU_ID"]
        if pd.notna(r["MOTIVO_ENTRADA"]):
            estados[s] = "dato_incompleto"
        elif s in por_sku:
            estados[s] = estado_sku(por_sku[s])
        else:
            estados[s] = "ok"

    kpis = calcular_kpis(estados, METRICAS.resumen(), len(resultados))
    alertas = evaluar_alertas(kpis, u, actualizar_historial=guardar_reportes and not skus)
    salida = {
        "trace_id": traza, "fecha": datetime.now(timezone.utc).isoformat(), "fuente_catalogo": fuente,
        "umbrales": umbrales_dict(u), "modo_seguro": seguro, "kpis": kpis, "alertas": alertas,
        "estados": estados, "resultados": [r.a_dict() for r in resultados],
        "auditoria_muestra": [r.sku_id for r in auditoria], "escalados_por_timeout": vencidos,
        "modelo_embedding": kb.modelo_embedding, "metricas": METRICAS.resumen(),
    }
    if guardar_reportes:
        REPORTS_DIR.mkdir(exist_ok=True)
        (REPORTS_DIR / "resultado_corrida.json").write_text(json.dumps(salida, indent=2, ensure_ascii=False, default=str),
                                                            encoding="utf-8")
        (REPORTS_DIR / "metricas.json").write_text(json.dumps({"kpis": kpis, "alertas": alertas,
                                                               "metricas": METRICAS.resumen()}, indent=2), encoding="utf-8")
        pd.DataFrame([{"SKU_ID": k, "ESTADO": v} for k, v in estados.items()]).to_csv(
            REPORTS_DIR / "estado_skus.csv", index=False)
        if len(cola_det):
            cola_det.to_csv(REPORTS_DIR / "cola_agente1.csv", index=False)
    log("INFO", "fin de corrida", **kpis)
    return salida


def calcular_kpis(estados: dict, met: dict, n_investigados: int) -> dict:
    total = len(estados)
    conteo = pd.Series(estados).value_counts().to_dict() if estados else {}
    resueltos = conteo.get("ok", 0) + conteo.get("corregido", 0)
    lat = met["latencias"]
    return {
        "skus_escaneados": total,
        "conteo_estados": conteo,
        # denominador = volumen completo escaneado, no la cola del Agente 2
        "tasa_finalizacion": round(resueltos / total, 4) if total else 0.0,
        "skus_a_revision_humana": total - resueltos,
        "tasa_inconsistencia_agente1": round(sum(1 for v in estados.values() if v not in ("ok", "dato_incompleto"))
                                             / total, 4) if total else 0.0,
        "latencia_agente1_s": lat.get("agente1_escaneo", {}).get("mediana_s", 0.0),
        "latencia_agente2_mediana_s": lat.get("agente2_por_sku", {}).get("mediana_s", 0.0),
        "latencia_agente2_p95_s": lat.get("agente2_por_sku", {}).get("p95_s", 0.0),
        "costo_llm_usd_por_sku_investigado": round(met["costo_usd_total"] / n_investigados, 4) if n_investigados else 0.0,
    }


def evaluar_alertas(kpis: dict, u: Umbrales, actualizar_historial: bool = True) -> list[dict]:
    alertas = []
    hist = json.loads(HIST_KPI.read_text()) if HIST_KPI.exists() else []
    previas = [h["tasa_finalizacion"] for h in hist[-4:]]
    if len(previas) >= 1:
        prom = sum(previas) / len(previas)
        caida_pp = 100 * (prom - kpis["tasa_finalizacion"])
        if caida_pp > u.alerta_caida_finalizacion_pp:
            alertas.append({"tipo": "caida_finalizacion", "severidad": "critica",
                            "detalle": f"cayó {caida_pp:.1f} pp vs promedio de {len(previas)} corridas previas",
                            "accion": "modo seguro (solo sugiere) + auditoría del 100% de la semana"})
            if actualizar_historial:
                fijar_modo("solo_sugiere", alertas[-1]["detalle"])
    if kpis["costo_llm_usd_por_sku_investigado"] > u.costo_max_usd_por_sku:
        alertas.append({"tipo": "costo", "severidad": "alta",
                        "detalle": f"USD {kpis['costo_llm_usd_por_sku_investigado']}/SKU > {u.costo_max_usd_por_sku}"})
    for a in alertas:
        METRICAS.inc(f"alerta_{a['tipo']}")
        log("ERROR" if a["severidad"] == "critica" else "WARNING", "alerta de KPI", **a)
    if actualizar_historial:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        hist.append({"fecha": datetime.now(timezone.utc).isoformat(), "tasa_finalizacion": kpis["tasa_finalizacion"]})
        HIST_KPI.write_text(json.dumps(hist[-12:], indent=2))
    return alertas
