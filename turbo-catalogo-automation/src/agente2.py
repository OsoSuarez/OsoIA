"""Agente 2 — Investigador y corrector de medidas.

Implementa el grafo de decisión del Blueprint (Pre-entrega 5) como una
máquina de estados explícita. Cada nodo es una función pura sobre el
estado del SKU, igual que un nodo de LangGraph; se puede portar a LangGraph
sin cambiar la lógica.

  buscar evidencia (RAG + foto + histórico)
     -> ¿hay alguna fuente?            no -> sin_fuente_confiable (o bloqueo propuesto)
     -> ¿2 oficiales difieren >15%?    sí -> fuentes_contradictorias (HITL, ambos candidatos)
     -> calcular confianza
     -> validación de salida (Capa 4)
     -> ¿alta + desvío<=30% + no alta rotación + Capa 4 ok?  sí -> corregir_directo
                                                             no -> pendiente_aprobacion (HITL)
"""
from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field

import pandas as pd

from . import llm
from .config import FUENTES_PERMITIDAS, DATA_DIR, Umbrales
from .detector import Rango
from .observabilidad import METRICAS, log
from .rag import ESQUEMA_EXTRACCION, BaseConocimiento, extraer_valores
from .vision import leer_rotulo

CAMPOS_LOGISTICOS = {"peso_kg", "largo_cm", "alto_cm", "ancho_cm"}
SEVERIDAD = ["ok", "corregido", "pendiente_aprobacion", "sin_fuente_confiable", "fuentes_contradictorias",
             "bloqueado_pendiente_aprobacion"]


@dataclass
class Evidencia:
    fuente: str
    valor: float
    cita: str  # archivo#chunk o archivo de imagen: trazabilidad
    oficial: bool


@dataclass
class Resultado:
    sku_id: str
    campo: str
    valor_actual: float
    desvio_vs_rango_pct: float
    rotacion: str
    estado: str = "inconsistente"
    accion_propuesta: str = "ninguna"
    valor_propuesto: float | None = None
    confianza: str | None = None
    diferencia_entre_fuentes_pct: float | None = None
    fuente_principal: str | None = None
    evidencias: list[Evidencia] = field(default_factory=list)
    motivo: str = ""
    umbrales_version: str = ""
    rango: tuple[float, float] | None = None

    def a_dict(self) -> dict:
        d = asdict(self)
        d["evidencias"] = [asdict(e) for e in self.evidencias]
        return d


def _dif_pct(a: float, b: float) -> float:
    base = max(abs(a), abs(b)) or 1.0
    return 100 * abs(a - b) / base


def _spread(vals: list[float]) -> float:
    return max((_dif_pct(a, b) for a in vals for b in vals), default=0.0)


class Investigador:
    def __init__(self, kb: BaseConocimiento, catalogo: pd.DataFrame, rangos: dict, u: Umbrales):
        self.kb, self.u, self.rangos = kb, u, rangos
        self.cat = catalogo.set_index("SKU_ID")
        h = DATA_DIR / "fuentes" / "historico_correcciones.csv"
        self.hist = pd.read_csv(h) if h.exists() else pd.DataFrame(columns=["SKU_ID", "CAMPO", "VALOR_CORREGIDO"])

    # ---- nodo 1: buscar evidencia ----
    def buscar_evidencia(self, sku_id: str, campo: str) -> list[Evidencia]:
        prod = self.cat.loc[sku_id]
        consulta = f"{sku_id} {prod['PRODUCT_NAME']} peso bruto dimensiones contenido neto"
        evid: list[Evidencia] = []
        for h in self.kb.buscar(consulta, sku_id, self.u):
            vals = extraer_valores(h["texto"])
            if all(v is None for v in vals.values()) and llm.disponible():
                # formato de tabla no reconocido (p.ej. otro layout de LlamaParse): extracción con el modelo
                vals = llm.llamar_json([{"role": "user", "content": f"Ficha técnica:\n{h['texto']}\n\n"
                                         f"Extraé los datos físicos del producto {sku_id}."}],
                                       ESQUEMA_EXTRACCION, "extraer_ficha")
            clave = "net_quantity_g" if campo == "net_quantity" else campo
            if vals.get(clave) is not None:
                evid.append(Evidencia(h["fuente"], float(vals[clave]), h["id"],
                                      FUENTES_PERMITIDAS[h["fuente"]]["oficial"]))
        foto = leer_rotulo(sku_id) if campo in CAMPOS_LOGISTICOS else None
        if foto and foto["legible"]:
            v = foto["peso_kg"] if campo == "peso_kg" else (
                dict(zip(["largo_cm", "ancho_cm", "alto_cm"], foto["dims"])).get(campo) if foto["dims"] else None)
            if v is not None:
                evid.append(Evidencia("foto_etiqueta", float(v), foto["archivo"], False))
        hh = self.hist[(self.hist["SKU_ID"] == sku_id) & (self.hist["CAMPO"] == campo)]
        for _, r in hh.iterrows():
            evid.append(Evidencia("historico_correcciones", float(r["VALOR_CORREGIDO"]),
                                  f"historico_correcciones.csv@{r['FECHA']}", False))
        return evid

    # ---- nodos 2..5 ----
    def investigar(self, fila: pd.Series) -> Resultado:
        u = self.u
        sku, campo = fila["SKU_ID"], fila["CAMPO"]
        prod = self.cat.loc[sku]
        rg: Rango | None = self.rangos.get((prod["GRUPO"], campo.upper()))
        res = Resultado(sku, campo, float(fila["VALOR"]), float(fila["DESVIO_PCT"]), str(prod["ROTACION"]),
                        umbrales_version=u.version, rango=(rg.lo, rg.hi) if rg else None)
        with METRICAS.cronometro("agente2_por_sku"):
            res.evidencias = self.buscar_evidencia(sku, campo)
            ev = res.evidencias

            if not ev:
                res.estado = "sin_fuente_confiable"
                grande = res.desvio_vs_rango_pct > u.desvio_rango_max_pct
                if campo in CAMPOS_LOGISTICOS and grande and bool(prod.get("ACTIVO", True)):
                    # dato logístico absurdo y sin forma de corregirlo: se PROPONE bloquear.
                    res.estado = "bloqueado_pendiente_aprobacion"
                    res.accion_propuesta = "bloquear_pendiente_aprobacion"
                    res.motivo = (f"sin fuente permitida y desvío {res.desvio_vs_rango_pct}% > "
                                  f"{u.desvio_rango_max_pct}%: riesgo de despacho/slotting")
                else:
                    res.motivo = "ninguna fuente permitida tiene el dato; se notifica al analista"
                return self._cerrar(res)

            oficiales = [e for e in ev if e.oficial]
            no_oficiales = [e for e in ev if not e.oficial]
            if len(oficiales) >= 2 and _spread([e.valor for e in oficiales]) > u.dif_fuentes_media_pct:
                res.estado = "fuentes_contradictorias"
                res.diferencia_entre_fuentes_pct = round(_spread([e.valor for e in oficiales]), 1)
                res.motivo = "dos fuentes oficiales difieren; decide el analista: " + "; ".join(
                    f"{e.fuente}={e.valor:g} ({e.cita})" for e in ev)
                return self._cerrar(res)

            if oficiales:
                propuesto = statistics.median(e.valor for e in oficiales)
                principal = oficiales[0].fuente
                dif = max((_dif_pct(propuesto, e.valor) for e in ev), default=0.0)
                conf = "alta" if dif <= u.dif_fuentes_alta_pct else "media" if dif <= u.dif_fuentes_media_pct else "baja"
            elif len(no_oficiales) >= 2 and _spread([e.valor for e in no_oficiales]) <= u.dif_fuentes_media_pct:
                propuesto = statistics.mean(e.valor for e in no_oficiales)
                principal, dif, conf = no_oficiales[0].fuente, _spread([e.valor for e in no_oficiales]), "media"
            else:
                propuesto = no_oficiales[0].valor
                principal, dif, conf = no_oficiales[0].fuente, _spread([e.valor for e in no_oficiales]), "baja"
            res.valor_propuesto = round(propuesto, 4)
            res.confianza = conf
            res.diferencia_entre_fuentes_pct = round(dif, 1)
            res.fuente_principal = principal

            # El rango marcó el SKU pero una fuente oficial confirma el valor
            # cargado: es un falso positivo del rango, se cierra sin tocar nada.
            if conf == "alta" and _dif_pct(res.valor_propuesto, res.valor_actual) <= u.dif_fuentes_alta_pct:
                res.estado, res.accion_propuesta = "ok", "ninguna"
                res.motivo = f"valor confirmado por {principal}: falso positivo del rango, sin cambios"
                METRICAS.inc("falsos_positivos_confirmados_por_fuente")
                return self._cerrar(res)

            # ---- Capa 4: validación de salida ----
            ok_salida, motivo_salida = self.validar_salida(res)
            if (conf == "alta" and res.desvio_vs_rango_pct <= u.desvio_rango_max_pct
                    and res.rotacion != "alta" and ok_salida):
                res.estado, res.accion_propuesta = "corregido", "corregir_directo"
                res.motivo = "fuente oficial, confianza alta, desvío ≤30%, no alta rotación"
            else:
                res.estado, res.accion_propuesta = "pendiente_aprobacion", "corregir_y_notificar"
                razones = []
                if conf != "alta":
                    razones.append(f"confianza {conf}")
                if res.desvio_vs_rango_pct > u.desvio_rango_max_pct:
                    razones.append(f"desvío {res.desvio_vs_rango_pct}% > {u.desvio_rango_max_pct}%")
                if res.rotacion == "alta":
                    razones.append("alta rotación")
                if not ok_salida:
                    razones.append(motivo_salida)
                res.motivo = "requiere aprobación: " + ", ".join(razones)
        return self._cerrar(res)

    def validar_salida(self, res: Resultado) -> tuple[bool, str]:
        u = self.u
        v = res.valor_propuesto
        if res.fuente_principal not in FUENTES_PERMITIDAS:
            return False, "fuente no permitida"
        if res.campo == "peso_kg" and not (u.peso_min_kg <= v <= u.peso_max_kg):
            return False, "valor propuesto fuera de rango físico"
        if res.campo in {"largo_cm", "alto_cm", "ancho_cm"} and not (u.dim_min_cm <= v <= u.dim_max_cm):
            return False, "valor propuesto fuera de rango físico"
        if res.rango and not (res.rango[0] <= v <= res.rango[1]):
            return False, "valor propuesto fuera del rango esperado de la subcategoría"
        return True, ""

    def _cerrar(self, res: Resultado) -> Resultado:
        METRICAS.inc(f"agente2_estado_{res.estado}")
        log("INFO", "SKU investigado", sku_id=res.sku_id, campo=res.campo, estado=res.estado,
            accion=res.accion_propuesta, confianza=res.confianza, valor_actual=res.valor_actual,
            valor_propuesto=res.valor_propuesto, fuentes=[e.cita for e in res.evidencias],
            umbrales_version=res.umbrales_version)
        return res


def estado_sku(resultados: list[Resultado]) -> str:
    return max((r.estado for r in resultados), key=SEVERIDAD.index)
