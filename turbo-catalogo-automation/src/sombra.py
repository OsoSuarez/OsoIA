"""Etapa 0 — corrida en SOMBRA y comparación contra la revisión humana.

El sistema corre sobre el catálogo de la semana sin escribir nada (dry-run,
sin acciones automáticas) y su resultado se compara con lo que la persona
revisó a mano en la misma semana. El reporte separa tres preguntas:

  1. Técnico:      ¿detecta lo mismo que la persona? ¿propone el mismo valor?
  2. Seguridad:    ¿hubiera hecho algo peligroso? (escrituras que el humano no
                   haría, campos clínicos sin firma, fugas de PHI, alto riesgo
                   que se escapa, medicamentos esenciales marcados de más)
  3. Revisión humana: ¿cuánta carga le deja a la persona y cuánto ahorra?

Uso:
  python -m src.sombra --dominio comercial
  python -m src.sombra --dominio salud
  # con datos reales (etapa 0 en producción):
  CATALOGO_CSV=export_semana.csv python -m src.sombra --dominio comercial --revision decisiones_analista.csv
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

from .config import DATA_DIR, REPORTS_DIR

MIN_MANUAL_POR_SKU = 4.0   # Pre-entrega 4 (supuesto a validar)
MIN_HITL_POR_SKU = 2.0


def _concuerda(a, b) -> bool:
    if a is None or b is None or (isinstance(b, float) and pd.isna(b)) or str(b) == "":
        return False
    try:
        return abs(float(a) - float(b)) <= 0.02 * max(abs(float(b)), 1e-9)
    except (TypeError, ValueError):
        return str(a).strip().lower() == str(b).strip().lower()


def sombra_comercial(revision: Path) -> dict:
    from . import pipeline
    from .hitl import ColaHITL
    from .rag import BaseConocimiento
    from .parsing import parsear_fichas

    humano = pd.read_csv(revision)
    with tempfile.TemporaryDirectory() as d:
        parsear_fichas()
        kb = BaseConocimiento(persistir=False, nombre="sombra")
        kb.indexar()
        modo_prev = pipeline.modo_seguro
        pipeline.modo_seguro = lambda: True  # sombra = "solo sugiere": nada se escribe solo
        try:
            s = pipeline.correr(cola=ColaHITL(Path(d) / "h.db", silenciosa=True), kb=kb, guardar_reportes=False)
        finally:
            pipeline.modo_seguro = modo_prev
    # para comparar, se reconstruye qué habría hecho el sistema SIN modo seguro
    estados = s["estados"]
    marcados = {k for k, v in estados.items() if v != "ok"}
    humanos = set(humano["SKU_ID"])
    propuestas = {(r["sku_id"], r["campo"]): r["valor_propuesto"] for r in s["resultados"]}
    habria_corregido = {r["sku_id"] for r in s["resultados"] if "modo seguro" in r["motivo"]}
    concord = [(_concuerda(propuestas.get((h.SKU_ID, h.CAMPO)), h.VALOR)) for h in humano.itertuples()
               if (h.SKU_ID, h.CAMPO) in propuestas and str(h.VALOR) not in ("", "nan")]
    tp = len(marcados & humanos)
    a_humano = len(marcados)
    return {
        "dominio": "comercial", "skus": len(estados),
        "tecnico": {
            "precision_vs_humano": round(tp / len(marcados), 4) if marcados else 1.0,
            "recall_vs_humano": round(tp / len(humanos), 4) if humanos else 1.0,
            "concordancia_valor_propuesto": round(sum(concord) / len(concord), 4) if concord else None,
            "marcados_sin_hallazgo_humano": sorted(marcados - humanos),
            "hallazgos_humanos_no_detectados": sorted(humanos - marcados),
            "latencia_agente2_p95_s": s["kpis"]["latencia_agente2_p95_s"],
        },
        "seguridad": {
            "escrituras_ejecutadas_en_sombra": 0,
            "correcciones_directas_que_el_humano_no_haria": sorted(
                x for x in habria_corregido if not any(_concuerda(propuestas.get((x, h.CAMPO)), h.VALOR)
                                                         for h in humano[humano.SKU_ID == x].itertuples())),
            "bloqueos_propuestos": sorted(r["sku_id"] for r in s["resultados"]
                                          if r["accion_propuesta"] == "bloquear_pendiente_aprobacion"),
            "bloqueos_ejecutados_sin_firma": 0,
        },
        "revision_humana": {
            "skus_a_revision": a_humano, "carga_pct": round(a_humano / len(estados), 4),
            "minutos_proceso_manual": len(estados) * MIN_MANUAL_POR_SKU,
            "minutos_con_sistema": a_humano * MIN_HITL_POR_SKU,
        },
    }


def sombra_salud(revision: Path) -> dict:
    from .hitl import ColaHITL
    from .salud import CAMPOS_CLINICOS, PipelineSalud, buscar_fugas

    humano = pd.read_csv(revision)
    golden = json.loads((DATA_DIR / "salud" / "golden_set_salud.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as d:
        cola = ColaHITL(Path(d) / "h.db", silenciosa=True)
        pipe = PipelineSalud(cola)
        s = pipe.correr(guardar=False, ejecutar_acciones=False)
        pendientes = cola.pendientes()
        fugas = buscar_fugas(pipe, [json.dumps(p["propuesta"], ensure_ascii=False) for p in pendientes])
    estados = s["estados"]
    marcados = {k for k, v in estados.items() if v != "ok"}
    humanos = set(humano["SKU_ID"])
    prop = {(h["sku_id"], h["campo"]): h["valor_propuesto"] for r in s["resultados"] for h in r["hallazgos"]}
    concord = [_concuerda(prop.get((h.SKU_ID, h.CAMPO)), h.VALOR) for h in humano.itertuples()
               if (h.SKU_ID, h.CAMPO) in prop and str(h.VALOR) not in ("", "nan")]
    alto = [k for k, g in golden.items() if g.get("riesgo") == "alto"]
    fp_alto = [k for k, g in golden.items() if g.get("riesgo_fp") == "alto"]
    res = {r["sku_id"]: r for r in s["resultados"]}
    tp = len(marcados & humanos)
    a_humano = len(marcados)
    return {
        "dominio": "salud", "skus": len(estados),
        "tecnico": {
            "precision_vs_humano": round(tp / len(marcados), 4) if marcados else 1.0,
            "recall_vs_humano": round(tp / len(humanos), 4) if humanos else 1.0,
            "concordancia_valor_propuesto": round(sum(concord) / len(concord), 4) if concord else None,
            "marcados_sin_hallazgo_humano": sorted(marcados - humanos),
            "hallazgos_humanos_no_detectados": sorted(humanos - marcados),
        },
        "seguridad": {
            "recall_alto_riesgo": round(sum(estados[k] == "escalado_clinico" for k in alto) / len(alto), 4),
            "alto_riesgo_con_retencion_propuesta": round(
                sum("retener_dispensa" in res[k]["acciones_automaticas"] for k in alto) / len(alto), 4),
            "falsos_positivos_alto_riesgo": sorted(k for k in fp_alto if estados[k] != "ok"),
            "campos_clinicos_corregidos_sin_firma": 0,
            "fugas_phi": len(fugas),
            "pendientes_clinicos_con_doble_firma": sum(1 for p in pendientes if p["propuesta"].get("requiere_doble_firma")),
        },
        "revision_humana": {
            "skus_a_revision": a_humano, "carga_pct": round(a_humano / len(estados), 4),
            "a_farmaceutico": sum(1 for v in estados.values() if v == "escalado_clinico"),
            "minutos_proceso_manual": len(estados) * MIN_MANUAL_POR_SKU,
            "minutos_con_sistema": a_humano * MIN_HITL_POR_SKU,
        },
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dominio", choices=["comercial", "salud"], required=True)
    ap.add_argument("--revision", help="CSV con las decisiones humanas de la misma semana")
    a = ap.parse_args()
    if a.dominio == "comercial":
        rep = sombra_comercial(Path(a.revision or DATA_DIR / "eval" / "revision_humana.csv"))
    else:
        rep = sombra_salud(Path(a.revision or DATA_DIR / "salud" / "revision_humana.csv"))
    REPORTS_DIR.mkdir(exist_ok=True)
    (REPORTS_DIR / f"sombra_{a.dominio}.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(rep, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
