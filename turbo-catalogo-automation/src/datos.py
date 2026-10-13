"""Carga del catálogo + Capa 1 (validación de entrada, determinística).

Fuente primaria: Snowflake (si hay credenciales en .env). Ruta alternativa:
CSV de muestra. La lectura de Snowflake va envuelta en reintentos con
backoff + jitter y un circuit breaker; si la base sigue sin responder, la
corrida se degrada al último snapshot en vez de fallar entera.
"""
from __future__ import annotations

import os
import random
import re
from pathlib import Path

import pandas as pd

from .config import DATA_DIR, Umbrales
from .observabilidad import METRICAS, log
from .resiliencia import CircuitBreaker, ErrorPermanente, ErrorTransitorio, con_fallback, reintentar

CB_SNOWFLAKE = CircuitBreaker("snowflake", umbral_fallos=3, enfriamiento_s=300)

COLUMNAS = ["SKU_ID", "PRODUCT_NAME", "SUBCATEGORY_NAME", "NET_QUANTITY", "MEASUREMENT_UNIT",
            "PESO_KG", "LARGO_CM", "ALTO_CM", "ANCHO_CM", "ROTACION", "PROVEEDOR_ID", "ACTIVO"]

# A la unidad estándar del pipeline (kg para masa, l para volumen).
FACTOR_UNIDAD = {"G": ("kg", 0.001), "GR": ("kg", 0.001), "KG": ("kg", 1.0),
                 "ML": ("l", 0.001), "L": ("l", 1.0), "LT": ("l", 1.0), "CC": ("l", 0.001)}


def _leer_snowflake() -> pd.DataFrame:
    """Lee la tabla de catálogo. El nombre de la tabla viene de .env:
    no se publica en el repo (dato interno)."""
    if os.getenv("SIMULAR_FALLA_FUENTE") and random.random() < float(os.getenv("SIMULAR_FALLA_FUENTE")):
        raise ErrorTransitorio("503 Service Unavailable (simulado)")
    if not os.getenv("SNOWFLAKE_ACCOUNT"):
        raise ErrorPermanente("sin credenciales de Snowflake en el entorno")
    import snowflake.connector  # import tardío: dependencia opcional

    tabla = os.environ["CATALOGO_TABLA"]
    try:
        conn = snowflake.connector.connect(
            account=os.environ["SNOWFLAKE_ACCOUNT"], user=os.environ["SNOWFLAKE_USER"],
            authenticator=os.getenv("SNOWFLAKE_AUTHENTICATOR", "externalbrowser"),
            warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"), role=os.getenv("SNOWFLAKE_ROLE"),
            login_timeout=20, network_timeout=60,
        )
    except Exception as e:  # noqa: BLE001
        raise ErrorTransitorio(str(e)) from e
    with conn:
        return pd.read_sql(f"SELECT {', '.join(COLUMNAS)} FROM {tabla}", conn)


@reintentar(intentos=4, base_s=0.2, max_s=2.0, servicio="snowflake")
def _leer_snowflake_resiliente() -> pd.DataFrame:
    return CB_SNOWFLAKE.llamar(_leer_snowflake)


def _leer_csv(path: Path | None = None) -> pd.DataFrame:
    """CATALOGO_CSV permite correr la etapa de sombra sobre un export real."""
    return pd.read_csv(path or os.getenv("CATALOGO_CSV") or DATA_DIR / "catalogo_muestra.csv")


def cargar_catalogo(usar_snowflake: bool | None = None) -> tuple[pd.DataFrame, str]:
    if usar_snowflake is None:
        usar_snowflake = bool(os.getenv("SNOWFLAKE_ACCOUNT") or os.getenv("SIMULAR_FALLA_FUENTE"))
    with METRICAS.cronometro("carga_catalogo"):
        if not usar_snowflake:
            df, ruta = _leer_csv(), "csv_muestra"
        else:
            df, ruta = con_fallback(_leer_snowflake_resiliente, _leer_csv, "carga_catalogo")
            ruta = "snowflake" if ruta == "primaria" else "csv_muestra(fallback)"
    log("INFO", "catálogo cargado", filas=len(df), fuente=ruta)
    METRICAS.inc("skus_leidos", len(df))
    return df, ruta


def estandarizar(df: pd.DataFrame) -> pd.DataFrame:
    """Lleva el contenido neto a unidad estándar (ej. 500 G -> 0,5 kg).
    Sin este paso 'Entraña 500 g' aparecía como el desvío más grande de la
    muestra real, cuando el dato era correcto."""
    out = df.copy()
    unidad = out["MEASUREMENT_UNIT"].astype(str).str.upper().str.strip()
    out["NET_STD"] = [
        (float(q) * FACTOR_UNIDAD[u][1]) if u in FACTOR_UNIDAD and pd.notna(q) else None
        for q, u in zip(out["NET_QUANTITY"], unidad)
    ]
    out["NET_STD_UNIDAD"] = [FACTOR_UNIDAD[u][0] if u in FACTOR_UNIDAD else None for u in unidad]
    return out


def validar_entrada(df: pd.DataFrame, u: Umbrales) -> pd.DataFrame:
    """Capa 1. Agrega MOTIVO_ENTRADA (None = pasa). Lo que falla no entra al
    escaneo: va a la cola 'dato_incompleto' para carga manual."""
    motivos = []
    for _, r in df.iterrows():
        m = []
        for c in ("PESO_KG", "LARGO_CM", "ALTO_CM", "ANCHO_CM"):
            if pd.isna(r[c]):
                m.append(f"falta {c}")
        if str(r["MEASUREMENT_UNIT"]).upper().strip() not in FACTOR_UNIDAD:
            m.append(f"unidad no reconocida '{r['MEASUREMENT_UNIT']}'")
        if pd.notna(r["PESO_KG"]) and not (u.peso_min_kg <= r["PESO_KG"] <= u.peso_max_kg):
            m.append(f"PESO_KG={r['PESO_KG']} fuera de rango físico [{u.peso_min_kg}, {u.peso_max_kg}]")
        for c in ("LARGO_CM", "ALTO_CM", "ANCHO_CM"):
            if pd.notna(r[c]) and not (u.dim_min_cm <= r[c] <= u.dim_max_cm):
                m.append(f"{c}={r[c]} fuera de rango físico [{u.dim_min_cm}, {u.dim_max_cm}]")
        if not isinstance(r["SUBCATEGORY_NAME"], str) or not r["SUBCATEGORY_NAME"].strip():
            m.append("sin subcategoría")
        motivos.append("; ".join(m) if m else None)
    out = df.copy()
    out["MOTIVO_ENTRADA"] = motivos
    n = int(out["MOTIVO_ENTRADA"].notna().sum())
    METRICAS.inc("skus_dato_incompleto", n)
    for _, r in out[out["MOTIVO_ENTRADA"].notna()].iterrows():
        log("WARNING", "SKU no pasa validación de entrada", sku_id=r["SKU_ID"], motivo=r["MOTIVO_ENTRADA"],
            estado="dato_incompleto")
    return out


NUM = r"(\d+(?:[.,]\d+)?)"


def a_float(s: str) -> float:
    return float(s.replace(".", "").replace(",", ".")) if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", s) \
        else float(s.replace(",", "."))
