"""Workflow auto-mejorable, con compuerta humana.

Cierra el ciclo: las decisiones del analista (registro de aprobaciones) y
las evaluaciones automáticas generan una PROPUESTA de nueva versión de
umbrales. La propuesta:
  1. se diagnostica a partir de los overrides (rechazos de lo que el agente
     propuso),
  2. se evalúa en sombra contra el golden set (misma eval que la compuerta de
     despliegue),
  3. solo se recomienda si mejora la exactitud y no empeora el recall, y
  4. la aplica el líder de Catálogo: modificar la tabla de rangos es una
     acción PROHIBIDA para el agente (Pre-entrega 3). El sistema se mejora a
     sí mismo proponiendo, no auto-modificándose en producción.

Uso:  python -m src.auto_mejora
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import replace

from .config import REPORTS_DIR, STATE_DIR, Umbrales, cargar_umbrales, umbrales_dict
from .detector import ES_PACK
from .hitl import ColaHITL
from .observabilidad import log


def diagnosticar(registro: list[dict], u: Umbrales) -> dict:
    total = len(registro)
    rechazos = [r for r in registro if r["resultado"] == "rechazado"]
    override_rate = round(len(rechazos) / total, 3) if total else 0.0
    motivos = Counter()
    for r in rechazos:
        j = r["justificacion"].lower()
        if re.search(r"\bpack\b|\d+\s*x\s*\d+", j):
            motivos["pack_en_subcategoria_de_unidades"] += 1
        elif "packaging" in j or "temporada" in j or "estacional" in j:
            motivos["cambio_de_packaging"] += 1
        else:
            motivos["otro"] += 1
    cambios, razones = {}, []
    if motivos["pack_en_subcategoria_de_unidades"] and not u.separar_packs:
        cambios["separar_packs"] = True
        razones.append(f"{motivos['pack_en_subcategoria_de_unidades']} rechazo(s) humano(s) de packs legítimos "
                       "marcados contra el rango de unidades sueltas")
    if motivos["cambio_de_packaging"]:
        cambios["iqr_k"] = round(u.iqr_k + 0.5, 2)
        razones.append("rechazos por cambio de packaging: ampliar tolerancia del rango")
    if override_rate > u.alerta_override_rate and not cambios:
        razones.append(f"override rate {override_rate} > {u.alerta_override_rate} sin patrón claro: revisión manual")
    return {"decisiones": total, "rechazos": len(rechazos), "override_rate": override_rate,
            "motivos": dict(motivos), "cambios": cambios, "razones": razones}


def proponer(cola: ColaHITL | None = None, evaluar_sombra: bool = True) -> dict:
    from .evals import evaluar

    cola = cola or ColaHITL()
    u = cargar_umbrales()
    diag = diagnosticar(cola.registro(), u)
    salida = {"umbrales_actuales": u.version, "diagnostico": diag}
    if not diag["cambios"]:
        salida["recomendacion"] = "sin cambios"
        return salida
    num = int(re.sub(r"\D", "", u.version) or 1) + 1
    candidata = replace(u, version=f"v{num}", **diag["cambios"])
    salida["umbrales_candidatos"] = umbrales_dict(candidata)
    if evaluar_sombra:
        base, cand = evaluar(u, "base"), evaluar(candidata, "candidata")
        comparar = ("exactitud_estado", "detector_precision", "detector_recall", "fidelidad_fuente")
        salida["eval_sombra"] = {k: {"actual": base[k], "candidata": cand[k]} for k in comparar}
        # mejora si sube exactitud o precisión SIN perder recall ni fidelidad
        # (perder recall = dejar pasar errores reales: nunca se acepta).
        no_empeora = all(cand[k] >= base[k] for k in ("exactitud_estado", "detector_recall")) and \
            (cand["fidelidad_fuente"] or 1) >= (base["fidelidad_fuente"] or 1)
        mejora = no_empeora and (cand["exactitud_estado"] > base["exactitud_estado"]
                                 or cand["detector_precision"] > base["detector_precision"])
        salida["recomendacion"] = ("aplicar (requiere aprobación del líder de Catálogo)" if mejora
                                   else "no aplicar: la candidata no mejora en la evaluación")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"umbrales_{candidata.version}_propuestos.json").write_text(
        json.dumps(umbrales_dict(candidata), indent=2), encoding="utf-8")
    REPORTS_DIR.mkdir(exist_ok=True)
    (REPORTS_DIR / "propuesta_recalibracion.json").write_text(json.dumps(salida, indent=2, ensure_ascii=False),
                                                              encoding="utf-8")
    log("INFO", "propuesta de recalibración", **{k: v for k, v in salida.items() if k != "umbrales_candidatos"})
    return salida


if __name__ == "__main__":
    print(json.dumps(proponer(), indent=2, ensure_ascii=False))
