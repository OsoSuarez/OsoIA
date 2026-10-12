"""Demo end-to-end reproducible (sin credenciales).

  1. Corrida semanal completa sobre el catálogo de muestra.
  2. El analista y el líder deciden sobre la cola HITL (decisiones simuladas,
     con justificación: quedan en el registro auditable).
  3. Pasan 49 h: se aplican las políticas de espera (escalamiento).
  4. Auto-mejora: el sistema lee los rechazos, propone umbrales v2 y los
     evalúa en sombra contra el golden set.
  5. Semana degradada: Snowflake no responde -> reintentos -> fallback.

Uso:  python run_demo.py            (agregar --reset para empezar de cero)
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import timedelta

from src import pipeline
from src.auto_mejora import proponer
from src.config import LOGS_DIR, REPORTS_DIR, STATE_DIR
from src.hitl import ColaHITL, ahora


def titulo(t: str) -> None:
    print(f"\n{'=' * 78}\n{t}\n{'=' * 78}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # consola de Windows
    if "--reset" in sys.argv:
        for d in (STATE_DIR, REPORTS_DIR, LOGS_DIR):
            shutil.rmtree(d, ignore_errors=True)
    cola = ColaHITL()

    titulo("1) Corrida semanal")
    s = pipeline.correr(cola=cola)
    k = s["kpis"]
    print(f"fuente: {s['fuente_catalogo']} | embeddings: {s['modelo_embedding']} | trace_id: {s['trace_id']}")
    print(f"SKUs escaneados: {k['skus_escaneados']} | estados: {k['conteo_estados']}")
    print(f"tasa de finalización (sin intervención humana): {k['tasa_finalizacion']:.1%} | "
          f"a revisión humana: {k['skus_a_revision_humana']}")
    print(f"latencia Agente 1: {k['latencia_agente1_s']} s | Agente 2 por SKU: mediana "
          f"{k['latencia_agente2_mediana_s']} s, p95 {k['latencia_agente2_p95_s']} s")
    for r in s["resultados"]:
        print(f"  {r['sku_id']:8} {r['campo']:13} {r['estado']:31} actual={r['valor_actual']:<8g} "
              f"propuesto={r['valor_propuesto'] if r['valor_propuesto'] is not None else '-':<7} "
              f"conf={r['confianza'] or '-':5} fuentes={[e['fuente'] for e in r['evidencias']]}")
    print(f"muestra de auditoría post-acción: {s['auditoria_muestra']}")

    titulo("2) Decisiones humanas sobre la cola HITL")
    decisiones = [
        ("YOG-004", "peso_kg", "aprobado", "Marina (analista)", "analista_catalogo",
         "Ficha del proveedor y catálogo del fabricante coinciden en 1,05 kg; 10,3 es un tipeo.", None),
        ("YOG-006", "alto_cm", "aprobado", "Marina (analista)", "analista_catalogo",
         "Alto cargado en decímetros; la ficha dice 23,5 cm.", None),
        ("SNK-003", "peso_kg", "aprobado", "Marina (analista)", "analista_catalogo",
         "Ficha y rótulo coinciden en 0,17 kg. Alta rotación: lo apruebo yo.", None),
        ("SNK-005", "peso_kg", "aprobado", "Marina (analista)", "analista_catalogo",
         "Rótulo y remedición física previa coinciden en 0,17 kg.", None),
        ("BAR-001", "net_quantity", "aprobado", "Marina (analista)", "analista_catalogo",
         "La ficha del proveedor confirma 16 g.", None),
        ("QUE-005", "peso_kg", "aprobado", "Marina (analista)", "analista_catalogo",
         "El fabricante describe el formato de 500 g; la ficha y el rótulo del producto real dicen 0,33 kg.", 0.33),
        ("BAR-006", "alto_cm", "rechazado", "Marina (analista)", "analista_catalogo",
         "No bloquear: es un pack 3x25 g, el dato está bien. No se puede comparar contra barritas sueltas.", None),
        ("BAR-006", "peso_kg", "rechazado", "Marina (analista)", "analista_catalogo",
         "Mismo caso: pack 3x25 g, el peso de 85 g es correcto.", None),
        ("BAR-006", "ancho_cm", "rechazado", "Marina (analista)", "analista_catalogo",
         "Mismo pack 3x25 g: el ancho también es correcto.", None),
        ("GSE-009", "peso_kg", "pedido_mas_evidencia", "Diego (líder)", "lider_catalogo",
         "Antes de bloquear pido remedición en el dark store; parece el peso del bulto de 12.", None),
    ]
    for sku, campo, dec, quien, rol, just, valor in decisiones:
        r = cola.decidir(sku, campo, dec, quien, rol, just, valor)
        print(f"  {sku:8} {campo:13} -> {r['resultado']:21} valor aplicado: {r['valor_aplicado']}")

    titulo("3) Pasan 49 h: políticas de espera")
    esc = cola.revisar_vencimientos(ahora() + timedelta(hours=49))
    print(f"escalados al líder por vencimiento: {esc}")
    pend = cola.pendientes()
    print("siguen pendientes:", [(p["sku_id"], p["estado"], "escalado" if p["escalado"] else "en plazo",
                                  "nunca vence" if p["vence"] is None else "vence") for p in pend])

    titulo("4) Auto-mejora: propuesta de recalibración + evaluación en sombra")
    prop = proponer(cola)
    print(json.dumps({k: v for k, v in prop.items() if k != "umbrales_candidatos"}, indent=2, ensure_ascii=False))

    titulo("5) Semana degradada: Snowflake no responde")
    os.environ["SIMULAR_FALLA_FUENTE"] = "1"
    s2 = pipeline.correr(cola=ColaHITL(STATE_DIR / "hitl_semana_degradada.db"), guardar_reportes=False)
    os.environ.pop("SIMULAR_FALLA_FUENTE")
    m = s2["metricas"]["contadores"]
    print(f"fuente usada: {s2['fuente_catalogo']} | reintentos: {m.get('reintentos_snowflake', 0)} | "
          f"fallbacks: {m.get('fallback_carga_catalogo', 0)} | la corrida terminó igual: "
          f"{s2['kpis']['skus_escaneados']} SKUs")
    print(f"\nReportes en {REPORTS_DIR} · logs estructurados en {LOGS_DIR / 'pipeline.jsonl'}")


if __name__ == "__main__":
    main()
