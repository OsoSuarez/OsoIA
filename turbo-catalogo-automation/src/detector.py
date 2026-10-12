"""Agente 1 — Detector de inconsistencias.

1) Rango esperado por subcategoría con IQR (como un boxplot): [Q1 - k·IQR, Q3 + k·IQR].
2) Validador de unidades: contenido neto vs. el peso que dice el nombre
   ("Entraña 500 g"), con la heurística v3 para packs "N x M g".
Salida: una fila por (SKU, campo) inconsistente, con desvío vs. rango en %.
Todo determinístico: no se gasta una llamada al modelo para detectar.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd

from .config import Umbrales
from .datos import a_float
from .observabilidad import METRICAS, log

# El contenido neto NO se compara contra el rango de la subcategoría (varía
# legítimamente entre formatos); se valida contra el nombre del producto.
CAMPOS = ["PESO_KG", "LARGO_CM", "ALTO_CM", "ANCHO_CM"]


@dataclass
class Rango:
    subcategoria: str
    campo: str
    lo: float
    hi: float
    n: int


ES_PACK = re.compile(r"\b\d+\s*[x×]\s*\d", re.I)


def asignar_grupo(df: pd.DataFrame, u: Umbrales) -> pd.DataFrame:
    """Grupo de comparación = subcategoría (+ ' [pack]' si separar_packs)."""
    out = df.copy()
    if u.separar_packs:
        es_pack = out["PRODUCT_NAME"].astype(str).str.contains(ES_PACK)
        out["GRUPO"] = out["SUBCATEGORY_NAME"] + es_pack.map({True: " [pack]", False: ""})
    else:
        out["GRUPO"] = out["SUBCATEGORY_NAME"]
    return out


def calcular_rangos(df: pd.DataFrame, u: Umbrales) -> dict[tuple[str, str], Rango]:
    rangos = {}
    for sub, g in df.groupby("GRUPO"):
        for c in CAMPOS:
            v = g[c].dropna().astype(float)
            if len(v) < u.min_skus_por_grupo:
                continue  # pocas muestras: el rango no es confiable, no se usa
            q1, q3 = v.quantile(0.25), v.quantile(0.75)
            iqr = q3 - q1
            # piso de tolerancia: en grupos muy homogéneos el IQR es casi 0 y
            # cualquier variación mínima de packaging sería "inconsistente".
            margen = max(u.iqr_k * iqr, 0.05 * abs(q3))
            rangos[(sub, c)] = Rango(sub, c, round(q1 - margen, 4), round(q3 + margen, 4), len(v))
    return rangos


def desvio_pct(valor: float, r: Rango) -> float:
    """Distancia al límite más cercano, en % del límite. 0 si está dentro."""
    if valor > r.hi:
        return round(100 * (valor - r.hi) / abs(r.hi), 1)
    if valor < r.lo:
        base = abs(r.lo) if r.lo != 0 else 1.0
        return round(100 * (r.lo - valor) / base, 1)
    return 0.0


# ---------------- validador de unidades (v3) ----------------
PACK = re.compile(r"(\d+)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*(?:g|gr|grs)\b", re.I)
SIMPLE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:g|gr|grs)\b", re.I)


def lecturas_nombre(nombre: str) -> list[float]:
    """Todas las lecturas posibles del peso en gramos según el nombre.
    "6x70 g" es ambiguo: a veces el total es 420, a veces el neto unitario
    es 70. Se devuelven ambas y se elige la más cercana al dato (v3)."""
    m = PACK.search(nombre)
    if m:
        n, w = int(m.group(1)), a_float(m.group(2))
        return [n * w, w]
    m = SIMPLE.findall(nombre)
    return [a_float(m[-1])] if m else []


def validar_unidades(df: pd.DataFrame, u: Umbrales) -> pd.DataFrame:
    filas = []
    sub = df[df["MEASUREMENT_UNIT"].astype(str).str.upper().isin(["G", "GR"])]
    for _, r in sub.iterrows():
        lect = lecturas_nombre(str(r["PRODUCT_NAME"]))
        if not lect:
            continue  # sin peso identificable en el nombre: no se juzga
        net = float(r["NET_QUANTITY"]) if pd.notna(r["NET_QUANTITY"]) else 0.0
        mejor = min(lect, key=lambda x: abs(x - net))
        dif = 100 * abs(net - mejor) / mejor
        if dif > u.tolerancia_nombre_pct:
            filas.append({"SKU_ID": r["SKU_ID"], "CAMPO": "net_quantity", "VALOR": net,
                          "ESPERADO_NOMBRE_G": mejor, "DESVIO_PCT": round(min(dif, 999.0), 1),
                          "MOTIVO": f"contenido neto {net:g} g vs '{mejor:g} g' en el nombre"})
    return pd.DataFrame(filas)


# ---------------- escaneo ----------------
def detectar(df: pd.DataFrame, u: Umbrales) -> tuple[pd.DataFrame, dict]:
    """df debe venir con GRUPO asignado (asignar_grupo)."""
    validos = df[df["MOTIVO_ENTRADA"].isna()]
    with METRICAS.cronometro("agente1_escaneo"):
        rangos = calcular_rangos(validos, u)
        filas = []
        for _, r in validos.iterrows():
            for c in CAMPOS:
                rg = rangos.get((r["GRUPO"], c))
                if rg is None or pd.isna(r[c]):
                    continue
                d = desvio_pct(float(r[c]), rg)
                if d > 0:
                    filas.append({
                        "SKU_ID": r["SKU_ID"], "CAMPO": c.lower(),
                        "VALOR": float(r[c]), "RANGO_LO": rg.lo, "RANGO_HI": rg.hi, "DESVIO_PCT": d,
                        "MOTIVO": f"{c}={r[c]} fuera de [{rg.lo}, {rg.hi}] ({rg.subcategoria}, n={rg.n})",
                    })
        incons = pd.DataFrame(filas)
        unidades = validar_unidades(validos, u)
        cola = pd.concat([incons, unidades], ignore_index=True) if len(unidades) else incons
    if len(cola):
        rot = df.set_index("SKU_ID")["ROTACION"]
        cola["ROTACION"] = cola["SKU_ID"].map(rot)
        # prioridad: alta rotación primero, luego mayor desvío
        cola["PRIORIDAD"] = cola["ROTACION"].map({"alta": 1, "media": 2, "baja": 3}).fillna(3).astype(int)
        cola = cola.sort_values(["PRIORIDAD", "DESVIO_PCT"], ascending=[True, False]).reset_index(drop=True)
    METRICAS.inc("skus_inconsistentes", cola["SKU_ID"].nunique() if len(cola) else 0)
    log("INFO", "escaneo completo", skus=len(validos), pares_rango=len(rangos),
        inconsistencias=len(cola), skus_marcados=int(cola["SKU_ID"].nunique()) if len(cola) else 0)
    return cola, rangos
