"""Variante healthcare: el mismo flujo aplicado a un catálogo de farmacia.

Qué cambia respecto del catálogo comercial (ver docs/healthcare.md):
- Campos CLÍNICOS (concentración, unidades por envase, condición de venta,
  cadena de frío): nunca se corrigen solos. Van a un farmacéutico y, si el
  riesgo es alto, requieren doble firma (farmacéutico + director técnico).
- Acción protectora automática y reversible: ante riesgo alto para el
  paciente se RETIENE LA DISPENSA (cada pedido pasa por el farmacéutico
  antes de salir). No corrige ni despublica: solo agrega un control.
- PHI: los reclamos de pacientes se redactan ANTES de indexar, loguear o
  mandar a un modelo. El original solo se lee con rol habilitado y cada
  lectura queda en la auditoría de accesos. Un guardia bloquea cualquier
  envío al modelo que todavía contenga PHI.
- Logística (peso, medidas): para corregir solo hacen falta DOS fuentes
  oficiales que coincidan (en comercial alcanza una).
- Políticas de espera más cortas: riesgo alto 2 h, medio 24 h, y escalan al
  director técnico.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .config import DATA_DIR, REPORTS_DIR, STATE_DIR, Umbrales
from .detector import calcular_rangos, desvio_pct
from .hitl import ColaHITL, EscritorCatalogo, ahora
from .observabilidad import METRICAS, configurar_logs, log, nueva_traza
from .rag import BaseConocimiento

SALUD_DIR = DATA_DIR / "salud"
CAMPOS_CLINICOS = ("concentracion_mg", "unidades_envase", "condicion_venta", "cadena_frio")
ORDEN_VENTA = {"venta_libre": 0, "bajo_receta": 1, "receta_archivada": 2}
SLA_HORAS = {"alto": 2, "medio": 24, "bajo": 48}

# ------------------------------------------------------------------ PHI
PATRONES_PHI = [
    ("NOMBRE", re.compile(r"(Paciente|Nombre|Sr\.?|Sra\.?)\s*:\s*[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)+")),
    ("CUIL", re.compile(r"\b(20|23|24|27|30|33|34)-\d{8}-\d\b")),
    ("DNI", re.compile(r"\bDNI\s*:?\s*\d{1,2}\.?\d{3}\.?\d{3}\b", re.I)),
    ("AFILIADO", re.compile(r"\b\d{9}/\d{2}\b")),
    ("FECHA_NAC", re.compile(r"fecha de nacimiento\s*:?\s*\d{1,2}/\d{1,2}/\d{2,4}", re.I)),
    ("TELEFONO", re.compile(r"\b(?:tel\.?|teléfono|cel\.?)?\s*\+?\d{2,4}[- ]\d{4}[- ]\d{4}\b", re.I)),
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    # dato de salud de una persona concreta (diagnóstico declarado)
    ("DIAGNOSTICO", re.compile(r"\b(diab[eé]tic[oa]|hipertens[oa]|embarazad[oa]|artritis|VIH|epilep\w+)(\s+tipo\s+\d)?", re.I)),
]


def redactar_phi(texto: str) -> tuple[str, list[str]]:
    hallados = []
    for etiqueta, patron in PATRONES_PHI:
        if patron.search(texto):
            hallados.append(etiqueta)
            texto = patron.sub(f"[{etiqueta}]", texto)
    return texto, hallados


def contiene_phi(texto: str) -> bool:
    return any(p.search(texto) for _, p in PATRONES_PHI)


def guardia_phi(texto: str) -> str:
    """Se aplica a todo lo que sale hacia un servicio externo (modelo,
    LlamaCloud, Slack). Si todavía hay PHI, no sale."""
    if contiene_phi(texto):
        METRICAS.inc("phi_bloqueada_en_salida")
        raise PermissionError("el texto contiene PHI: no puede salir hacia un servicio externo")
    return texto


class AuditoriaAccesos:
    """Quién leyó un dato con PHI, cuándo, para qué y si se le permitió."""

    ROLES_LECTURA_PHI = {"servicio_redaccion", "farmaceutico", "director_tecnico", "dpo"}

    def __init__(self, path: Path | None = None):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path or STATE_DIR / "auditoria_accesos.db", check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS accesos (ts TEXT, actor TEXT, rol TEXT, recurso TEXT,
                           proposito TEXT, resultado TEXT)""")

    def leer_reclamo_crudo(self, rec_id: str, actor: str, rol: str, proposito: str) -> dict:
        permitido = rol in self.ROLES_LECTURA_PHI and bool(proposito.strip())
        self.db.execute("INSERT INTO accesos VALUES (?,?,?,?,?,?)",
                        (ahora().isoformat(), actor, rol, f"reclamo/{rec_id}", proposito,
                         "permitido" if permitido else "denegado"))
        self.db.commit()
        METRICAS.inc("accesos_phi_" + ("permitidos" if permitido else "denegados"))
        if not permitido:
            raise PermissionError(f"rol '{rol}' sin acceso a PHI")
        return json.loads((SALUD_DIR / "reclamos" / f"{rec_id}.json").read_text(encoding="utf-8"))

    def registros(self) -> list[tuple]:
        return list(self.db.execute("SELECT * FROM accesos ORDER BY ts"))


# ------------------------------------------------------------------ lectura de valores
def _num(s: str) -> float:
    return float(s.replace(",", "."))


def concentracion_mg_de(texto: str) -> float | None:
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(mcg|mg|g)\b(?!\s*/\s*ml)", texto, re.I)
    if not m:
        return None
    v, u = _num(m.group(1)), m.group(2).lower()
    return v / 1000 if u == "mcg" else v * 1000 if u == "g" else v


def unidades_de(texto: str) -> int | None:
    m = re.search(r"x\s*(\d+)\s*(comprimidos|cápsulas|capsulas|dosis|sobres|ampollas)", texto, re.I)
    return int(m.group(1)) if m else None


def leer_prospecto(texto: str) -> dict:
    def campo(nombre):
        m = re.search(rf"\|\s*{nombre}\s*\|\s*([^|]+)\|", texto, re.I)
        return m.group(1).strip() if m else None
    venta = (campo("Condición de venta") or "").lower()
    cond = "receta_archivada" if "archivada" in venta else "bajo_receta" if "receta" in venta else \
        "venta_libre" if "libre" in venta else None
    conserv = (campo("Conservación") or "").lower()
    peso = campo("Peso bruto unidad")
    return {
        "concentracion_mg": concentracion_mg_de(campo("Concentración") or ""),
        "unidades_envase": int(campo("Unidades por envase")) if (campo("Unidades por envase") or "").isdigit() else None,
        "condicion_venta": cond,
        "cadena_frio": ("2 y 8" in conserv or "refriger" in conserv) if conserv else None,
        "peso_kg": _num(peso.split()[0]) if peso else None,
    }


# ------------------------------------------------------------------ decisión
@dataclass
class Hallazgo:
    campo: str
    valor_actual: object
    valor_propuesto: object
    riesgo: str
    motivo: str
    fuentes: list[str] = field(default_factory=list)


@dataclass
class ResultadoSalud:
    sku_id: str
    estado: str
    riesgo: str | None
    hallazgos: list[Hallazgo]
    acciones_automaticas: list[str]
    motivo: str


def _bool(v) -> bool:
    return str(v).strip().lower() in ("true", "1", "si", "sí")


class PipelineSalud:
    def __init__(self, cola: ColaHITL, u: Umbrales | None = None, persistir: bool = False):
        self.u = u or Umbrales(version="salud-v1")
        self.cola = cola
        self.auditoria = AuditoriaAccesos(STATE_DIR / "auditoria_accesos_salud.db")
        self.escritor = EscritorCatalogo("correcciones_salud.csv")
        self.kb = BaseConocimiento(persistir=persistir, nombre="salud")
        self.phi_detectada: list[dict] = []

    # 1) base de conocimiento: prospectos (oficial) + reclamos redactados (no oficial)
    def indexar(self) -> int:
        docs = [(p.read_text(encoding="utf-8"), "prospecto", p.name)
                for p in sorted((SALUD_DIR / "prospectos").glob("*.md"))]
        for p in sorted((SALUD_DIR / "reclamos").glob("*.json")):
            rec = self.auditoria.leer_reclamo_crudo(p.stem, "pipeline", "servicio_redaccion",
                                                    "redacción de PHI antes de indexar")
            texto, hallados = redactar_phi(rec["texto"])
            self.phi_detectada.append({"reclamo": rec["id"], "tipos": hallados})
            cons = "sí" if rec.get("consentimiento_contacto") else "NO (no contactar al paciente)"
            docs.append((f"# Reclamo {rec['id']} sobre {rec['sku_id']}\nConsentimiento de contacto: {cons}\n\n{texto}",
                         "reclamo_cliente", p.name))
            log("INFO", "reclamo redactado e indexado", reclamo=rec["id"], sku_id=rec["sku_id"], phi_tipos=hallados)
        return self.kb.indexar_textos(docs)

    def evaluar_sku(self, r: pd.Series, rangos: dict) -> ResultadoSalud:
        sku = r["SKU_ID"]
        nombre = str(r["PRODUCT_NAME"])
        hallazgos: list[Hallazgo] = []
        conc_nombre = concentracion_mg_de(nombre)
        # Capa 1: la concentración es obligatoria cuando el nombre la expresa en mg/g/mcg
        if conc_nombre is not None and pd.isna(r["CONCENTRACION_MG"]):
            return ResultadoSalud(sku, "dato_incompleto", "medio", [], [], "falta la concentración")

        hits = self.kb.buscar(f"{sku} {nombre} concentración condición de venta conservación", sku, self.u)
        prosp = next((h for h in hits if h["fuente"] == "prospecto"), None)
        reclamos = [h for h in hits if h["fuente"] == "reclamo_cliente"]
        p = leer_prospecto(prosp["texto"]) if prosp else {}
        cita = [prosp["id"]] if prosp else []

        # --- campos clínicos: catálogo vs prospecto (oficial) vs nombre
        actual = float(r["CONCENTRACION_MG"]) if pd.notna(r["CONCENTRACION_MG"]) else None
        ref = p.get("concentracion_mg") or conc_nombre
        if actual is not None and ref and abs(actual - ref) / ref > 0.01:
            ratio = max(actual, ref) / min(actual, ref) if min(actual, ref) else 99
            hallazgos.append(Hallazgo("concentracion_mg", actual, ref, "alto" if ratio >= 2 else "medio",
                                      f"concentración {actual:g} mg vs {ref:g} mg (prospecto/nombre)", cita))
        uds_ref = p.get("unidades_envase") or unidades_de(nombre)
        if uds_ref and int(r["UNIDADES_ENVASE"]) != uds_ref:
            hallazgos.append(Hallazgo("unidades_envase", int(r["UNIDADES_ENVASE"]), uds_ref, "medio",
                                      f"{r['UNIDADES_ENVASE']} unidades vs {uds_ref} del prospecto", cita))
        if p.get("condicion_venta") and ORDEN_VENTA[r["CONDICION_VENTA"]] < ORDEN_VENTA[p["condicion_venta"]]:
            hallazgos.append(Hallazgo("condicion_venta", r["CONDICION_VENTA"], p["condicion_venta"], "alto",
                                      "publicado con una condición de venta menos restrictiva que la aprobada", cita))
        if p.get("cadena_frio") and not _bool(r["CADENA_FRIO"]):
            hallazgos.append(Hallazgo("cadena_frio", False, True, "alto",
                                      "el prospecto exige 2–8 °C y el catálogo no marca cadena de frío", cita))
        # reclamo de paciente sobre un dato clínico: siempre lo ve un farmacéutico
        for rc in reclamos:
            if re.search(r"\bmg\b|dosis|concentraci|fr[ií]o|tibia|conservad|vencid", rc["texto"], re.I):
                if not any(h.campo != "peso_kg" for h in hallazgos):
                    hallazgos.append(Hallazgo("concentracion_mg" if "mg" in rc["texto"] else "cadena_frio",
                                              None, None, "medio", "reclamo de paciente sobre un dato clínico",
                                              [rc["id"]]))
                else:
                    hallazgos[0].fuentes.append(rc["id"])

        # --- logística: en salud hacen falta 2 fuentes oficiales para corregir solo
        peso = float(r["PESO_KG"])
        rg = rangos.get(("Farmacia", "PESO_KG"))
        if p.get("peso_kg") and abs(peso - p["peso_kg"]) / p["peso_kg"] > self.u.dif_fuentes_alta_pct / 100:
            hallazgos.append(Hallazgo("peso_kg", peso, p["peso_kg"], "bajo",
                                      "una sola fuente oficial: en salud no alcanza para corregir solo", cita))
        elif not p and rg and desvio_pct(peso, rg) > 0:
            hallazgos.append(Hallazgo("peso_kg", peso, None, "bajo",
                                      f"peso fuera de rango [{rg.lo}, {rg.hi}] y sin prospecto", []))

        if not hallazgos:
            return ResultadoSalud(sku, "ok", None, [], [], "sin hallazgos")
        clinicos = [h for h in hallazgos if h.campo in CAMPOS_CLINICOS]
        riesgo = max((h.riesgo for h in hallazgos), key=["bajo", "medio", "alto"].index)
        acciones = []
        if clinicos:
            estado = "escalado_clinico"
            if riesgo == "alto":
                acciones.append("retener_dispensa")
        else:
            estado = "pendiente_aprobacion"
        return ResultadoSalud(sku, estado, riesgo, hallazgos, acciones,
                              "; ".join(h.motivo for h in hallazgos))

    def correr(self, catalogo: Path | None = None, guardar: bool = True, ejecutar_acciones: bool | None = None) -> dict:
        ejecutar_acciones = guardar if ejecutar_acciones is None else ejecutar_acciones
        configurar_logs()
        METRICAS.__init__()
        traza = nueva_traza()
        t0 = time.perf_counter()
        df = pd.read_csv(catalogo or SALUD_DIR / "catalogo_farmacia.csv")
        n_chunks = self.indexar()
        tmp = df.assign(GRUPO="Farmacia", MOTIVO_ENTRADA=None)
        rangos = calcular_rangos(tmp, self.u)
        resultados = []
        for _, r in df.iterrows():
            with METRICAS.cronometro("salud_por_sku"):
                res = self.evaluar_sku(r, rangos)
            resultados.append(res)
            for accion in res.acciones_automaticas:
                if accion == "retener_dispensa" and ejecutar_acciones:
                    self.escritor.retener_dispensa(res.sku_id, res.motivo)
            for h in res.hallazgos:
                es_clinico = h.campo in CAMPOS_CLINICOS
                self.cola.encolar({
                    "sku_id": res.sku_id, "campo": h.campo, "estado": res.estado if es_clinico else "pendiente_aprobacion",
                    "accion_propuesta": "corregir_con_firma_farmaceutica" if es_clinico else "corregir_y_notificar",
                    "valor_actual": h.valor_actual, "valor_propuesto": h.valor_propuesto, "riesgo": h.riesgo,
                    "motivo": h.motivo, "evidencias": [{"cita": c, "fuente": "prospecto"} for c in h.fuentes],
                    "dominio": "salud", "revisor": "farmaceutico", "escala_a": "director_tecnico",
                    "sla_horas": SLA_HORAS[h.riesgo], "requiere_doble_firma": es_clinico and h.riesgo == "alto",
                    "rotacion": r["ROTACION"], "desvio_vs_rango_pct": None,
                }, self.u)
            log("INFO", "SKU de farmacia evaluado", sku_id=res.sku_id, estado=res.estado, riesgo=res.riesgo,
                acciones=res.acciones_automaticas, campos=[h.campo for h in res.hallazgos])
        estados = {r.sku_id: r.estado for r in resultados}
        salida = {
            "trace_id": traza, "dominio": "salud", "skus": len(df), "chunks_indexados": n_chunks,
            "estados": estados, "conteo": pd.Series(estados).value_counts().to_dict(),
            "resultados": [{"sku_id": r.sku_id, "estado": r.estado, "riesgo": r.riesgo,
                            "acciones_automaticas": r.acciones_automaticas, "motivo": r.motivo,
                            "hallazgos": [h.__dict__ for h in r.hallazgos]} for r in resultados],
            "phi_redactada": self.phi_detectada, "latencia_total_s": round(time.perf_counter() - t0, 3),
            "metricas": METRICAS.resumen(),
        }
        if guardar:
            REPORTS_DIR.mkdir(exist_ok=True)
            (REPORTS_DIR / "resultado_salud.json").write_text(
                json.dumps(salida, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return salida


# ------------------------------------------------------------------ evaluación healthcare
def textos_phi_crudos() -> list[str]:
    """Fragmentos de PHI reales de los reclamos (para buscar fugas)."""
    frag = []
    for p in (SALUD_DIR / "reclamos").glob("*.json"):
        t = json.loads(p.read_text(encoding="utf-8"))["texto"]
        for _, patron in PATRONES_PHI:
            frag += [m.group(0) for m in patron.finditer(t)]
    return frag


def buscar_fugas(pipe: PipelineSalud, extras: list[str]) -> list[str]:
    frag = [f for f in textos_phi_crudos() if len(f) >= 6]
    corpus = "\n".join(pipe.kb.col.get()["documents"]) + "\n" + "\n".join(extras)
    return [f for f in frag if f in corpus]


def evaluar_salud(tmpdir: Path) -> dict:
    golden = json.loads((SALUD_DIR / "golden_set_salud.json").read_text(encoding="utf-8"))
    cola = ColaHITL(tmpdir / "hitl_salud.db", silenciosa=True)
    pipe = PipelineSalud(cola)
    pipe.escritor.archivo = str(tmpdir / "escrituras_eval.csv")  # ruta absoluta: no toca reports/
    salida = pipe.correr(guardar=False, ejecutar_acciones=True)
    escrituras = pd.read_csv(tmpdir / "escrituras_eval.csv") if (tmpdir / "escrituras_eval.csv").exists() \
        else pd.DataFrame(columns=["campo", "aprobado_por_humano"])
    estados = salida["estados"]
    esperado = {s: golden.get(s, {"estado": "ok"})["estado"] for s in estados}
    alto = [s for s, g in golden.items() if g.get("riesgo") == "alto"]
    fp_alto = [s for s, g in golden.items() if g.get("riesgo_fp") == "alto"]
    res_por_sku = {r["sku_id"]: r for r in salida["resultados"]}
    pendientes = cola.pendientes()
    # fugas: base vectorial + cola humana + logs + salida de la corrida
    from .config import LOGS_DIR
    logs = (LOGS_DIR / "pipeline.jsonl").read_text(encoding="utf-8") if (LOGS_DIR / "pipeline.jsonl").exists() else ""
    fugas = buscar_fugas(pipe, [json.dumps(p["propuesta"], ensure_ascii=False) for p in pendientes]
                         + [logs, json.dumps(salida, ensure_ascii=False, default=str)])
    reporte = {
        "skus": len(estados),
        "exactitud_estado": round(sum(estados[s] == esperado[s] for s in estados) / len(estados), 4),
        "recall_alto_riesgo": round(sum(estados[s] == "escalado_clinico" for s in alto) / len(alto), 4),
        "retencion_dispensa_en_alto_riesgo": round(
            sum("retener_dispensa" in res_por_sku[s]["acciones_automaticas"] for s in alto) / len(alto), 4),
        "falsos_positivos_alto_riesgo": sum(estados[s] != "ok" for s in fp_alto),
        # escrituras que hizo el sistema sin firma humana sobre campos clínicos (tiene que ser 0)
        "autocorrecciones_clinicas": int(((escrituras["campo"].isin(CAMPOS_CLINICOS))
                                          & (escrituras["aprobado_por_humano"].astype(str) != "True")).sum()),
        "acciones_automaticas_ejecutadas": escrituras["campo"].value_counts().to_dict(),
        "doble_firma_requerida_en_alto": all(p["propuesta"]["requiere_doble_firma"] for p in pendientes
                                            if p["propuesta"]["riesgo"] == "alto" and p["campo"] in CAMPOS_CLINICOS),
        "fugas_phi": len(fugas), "detalle_fugas": fugas,
        "reclamos_con_phi_redactada": sum(1 for x in salida["phi_redactada"] if x["tipos"]),
        "errores": [{"sku_id": s, "esperado": esperado[s], "obtenido": estados[s]} for s in estados
                    if estados[s] != esperado[s]],
        "conteo": salida["conteo"],
    }
    return reporte


if __name__ == "__main__":
    import sys
    import tempfile

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    with tempfile.TemporaryDirectory() as d:
        rep = evaluar_salud(Path(d))
    print(json.dumps(rep, indent=2, ensure_ascii=False))
    ok = (rep["exactitud_estado"] >= 0.95 and rep["recall_alto_riesgo"] == 1.0 and rep["fugas_phi"] == 0
          and rep["falsos_positivos_alto_riesgo"] == 0)
    print("GATE SALUD:", "OK" if ok else "FALLA")
    sys.exit(0 if ok else 1)
