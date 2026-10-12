"""Evaluaciones automáticas.

Offline (antes de desplegar un cambio de prompt, umbral o modelo):
  corre el pipeline completo contra el golden set y mide
  - exactitud de estado por SKU (¿terminó en el estado correcto?)
  - precisión y recall del detector (positivo = SKU con problema real)
  - exactitud del valor propuesto (±2%)
  - fidelidad a la fuente: un jurado verifica que cada valor propuesto esté
    efectivamente en la evidencia citada (rule-based offline; LLM-as-judge
    si hay API key).
  La compuerta (gate) falla si la exactitud baja del mínimo: un cambio que
  empeora el sistema no se despliega.

Online (producción): el mismo jurado corre sobre la muestra de auditoría
semanal (15% de las correcciones directas) antes de que la vea el líder.

Uso:  python -m src.evals [--umbrales v2.json] [--min-exactitud 0.9]
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from . import llm
from .config import DATA_DIR, REPORTS_DIR, Umbrales
from .hitl import ColaHITL
from .observabilidad import log
from .rag import BaseConocimiento

PROBLEMA = lambda e: e != "ok"  # noqa: E731

ESQUEMA_JURADO = {
    "type": "object",
    "properties": {
        "respaldado": {"type": "boolean", "description": "¿La evidencia contiene literalmente el valor propuesto?"},
        "explicacion": {"type": "string"},
    },
    "required": ["respaldado", "explicacion"],
}


def _texto_evidencia(kb: BaseConocimiento, cita: str) -> str:
    if "#" in cita:
        r = kb.col.get(ids=[cita])
        return r["documents"][0] if r["documents"] else ""
    if cita.endswith(".png"):
        grab = json.loads((DATA_DIR / "fotos" / "lecturas_grabadas.json").read_text(encoding="utf-8"))
        return grab.get(cita[:-4], {}).get("texto_detectado", "")
    if cita.startswith("historico_correcciones"):
        return (DATA_DIR / "fuentes" / "historico_correcciones.csv").read_text(encoding="utf-8")
    return ""


def _variantes(v: float, campo: str) -> list[str]:
    out = {f"{v:g}", f"{v:g}".replace(".", ",")}
    if campo == "peso_kg":
        out |= {f"{v:.2f}".replace(".", ","), f"{v:.2f}"}
    return list(out)


def jurado(kb: BaseConocimiento, res: dict) -> dict:
    """¿El valor propuesto está respaldado por la evidencia citada?"""
    if res.get("valor_propuesto") is None or not res.get("evidencias"):
        return {"aplica": False}
    cita = next((e["cita"] for e in res["evidencias"] if e["fuente"] == res["fuente_principal"]),
                res["evidencias"][0]["cita"])
    texto = _texto_evidencia(kb, cita)
    if llm.disponible():
        v = llm.llamar_json([{"role": "user", "content":
                              f"Evidencia:\n{texto}\n\nValor propuesto para {res['campo']} del SKU {res['sku_id']}: "
                              f"{res['valor_propuesto']}. ¿La evidencia respalda ese valor? Juzgá solo con la evidencia."}],
                            ESQUEMA_JURADO, "jurado_fidelidad")
        return {"aplica": True, "respaldado": v["respaldado"], "explicacion": v["explicacion"], "juez": "llm", "cita": cita}
    ok = any(var in texto for var in _variantes(res["valor_propuesto"], res["campo"]))
    if not ok and res["campo"] == "net_quantity":
        ok = f"{res['valor_propuesto']:g} g" in texto
    return {"aplica": True, "respaldado": ok, "explicacion": "coincidencia literal en la evidencia citada"
            if ok else "el valor no aparece en la evidencia citada", "juez": "reglas", "cita": cita}


def evaluar(u: Umbrales | None = None, etiqueta: str = "actual") -> dict:
    from . import pipeline  # import tardío para evitar ciclo

    u = u or Umbrales()
    golden = json.loads((DATA_DIR / "eval" / "golden_set.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        kb = BaseConocimiento(persistir=False, nombre=f"eval_{etiqueta}")
        from .parsing import parsear_fichas
        parsear_fichas()
        kb.indexar()
        cola = ColaHITL(Path(tmp) / "hitl_eval.db", silenciosa=True)
        modo_prev = pipeline.modo_seguro
        pipeline.modo_seguro = lambda: False  # la eval mide el comportamiento nominal
        try:
            salida = pipeline.correr(u=u, cola=cola, kb=kb, guardar_reportes=False)
        finally:
            pipeline.modo_seguro = modo_prev
    estados = salida["estados"]
    esperado = {s: golden.get(s, {"estado": "ok"})["estado"] for s in estados}

    aciertos = {s: estados[s] == esperado[s] for s in estados}
    tp = sum(1 for s in estados if PROBLEMA(esperado[s]) and PROBLEMA(estados[s]))
    fp = sum(1 for s in estados if not PROBLEMA(esperado[s]) and PROBLEMA(estados[s]))
    fn = sum(1 for s in estados if PROBLEMA(esperado[s]) and not PROBLEMA(estados[s]))

    valores, fidelidad = [], []
    for r in salida["resultados"]:
        g = golden.get(r["sku_id"], {})
        if "valor" in g and g.get("campo") == r["campo"] and r["valor_propuesto"] is not None:
            valores.append(abs(r["valor_propuesto"] - g["valor"]) <= 0.02 * abs(g["valor"]))
        j = jurado(kb, r)
        if j["aplica"]:
            fidelidad.append({"sku_id": r["sku_id"], "campo": r["campo"], **j})

    errores = [{"sku_id": s, "esperado": esperado[s], "obtenido": estados[s],
                "nota": golden.get(s, {}).get("motivo", "")} for s in estados if not aciertos[s]]
    reporte = {
        "etiqueta": etiqueta, "umbrales_version": u.version, "skus": len(estados),
        "exactitud_estado": round(sum(aciertos.values()) / len(estados), 4),
        "detector_precision": round(tp / (tp + fp), 4) if tp + fp else 1.0,
        "detector_recall": round(tp / (tp + fn), 4) if tp + fn else 1.0,
        "exactitud_valor_propuesto": round(sum(valores) / len(valores), 4) if valores else None,
        "fidelidad_fuente": round(sum(f["respaldado"] for f in fidelidad) / len(fidelidad), 4) if fidelidad else None,
        "verdaderos_positivos": tp, "falsos_positivos": fp, "falsos_negativos": fn,
        "errores": errores, "jurado": fidelidad, "kpis_corrida": salida["kpis"],
    }
    log("INFO", "evaluación offline", **{k: v for k, v in reporte.items() if k not in ("jurado", "errores")})
    return reporte


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--umbrales", help="JSON con una versión candidata de umbrales")
    ap.add_argument("--min-exactitud", type=float, default=0.90)
    a = ap.parse_args()
    u = Umbrales(**json.loads(Path(a.umbrales).read_text())) if a.umbrales else Umbrales()
    rep = evaluar(u, etiqueta=u.version)
    REPORTS_DIR.mkdir(exist_ok=True)
    (REPORTS_DIR / f"eval_{u.version}.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in rep.items() if k not in ("jurado", "kpis_corrida")}, indent=2, ensure_ascii=False))
    if rep["exactitud_estado"] < a.min_exactitud or (rep["fidelidad_fuente"] or 1) < 1.0:
        print(f"GATE: FALLA (exactitud {rep['exactitud_estado']} < {a.min_exactitud} o fidelidad < 100%)")
        sys.exit(1)
    print("GATE: OK")


if __name__ == "__main__":
    main()
