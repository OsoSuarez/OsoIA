"""Umbrales y parámetros del pipeline.

Todos los números salen del Blueprint de Célula Híbrida (Pre-entregas 3 y 4)
para que el código y el documento usen exactamente las mismas reglas.
Se versionan: cada corrida registra UMBRALES_VERSION en los logs y en cada
corrección, así una auditoría sabe contra qué reglas se evaluó un SKU.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
# Overridables por entorno (los tests usan carpetas temporales).
REPORTS_DIR = Path(os.getenv("TURBO_REPORTS_DIR", ROOT / "reports"))
LOGS_DIR = Path(os.getenv("TURBO_LOGS_DIR", ROOT / "logs"))
STATE_DIR = Path(os.getenv("TURBO_STATE_DIR", ROOT / ".state"))  # base vectorial, cola HITL. No se versiona.


@dataclass(frozen=True)
class Umbrales:
    version: str = "v1"
    # Capa 1 — rango físico posible (unidad estándar kg / cm)
    peso_min_kg: float = 0.001
    peso_max_kg: float = 50.0
    dim_min_cm: float = 0.5
    dim_max_cm: float = 200.0
    # Detector — rango esperado por subcategoría (IQR, igual que un boxplot)
    iqr_k: float = 1.5
    min_skus_por_grupo: int = 4  # con menos muestras el rango no es confiable
    # Si True, los packs ("3x25 g") se comparan contra otros packs de su
    # subcategoría y no contra las unidades sueltas. Lo propone auto_mejora.py.
    separar_packs: bool = False
    # Desvío vs rango: > este % fuera del límite => siempre condicional (firma humana)
    desvio_rango_max_pct: float = 30.0
    # Diferencia entre fuentes (Agente 2)
    dif_fuentes_alta_pct: float = 5.0
    dif_fuentes_media_pct: float = 15.0  # > 15% entre fuentes => fuentes_contradictorias
    # Validador de unidades en gramos
    tolerancia_nombre_pct: float = 10.0
    # RAG
    top_k: int = 4
    similitud_min: float = 0.15
    # HITL — políticas de espera
    hitl_timeout_horas_correccion: int = 48  # vence => escala al líder (nunca auto-aprueba)
    hitl_bloqueo_expira: bool = False  # un bloqueo de SKU activo nunca expira solo
    # Auditoría post-acción
    fraccion_auditoria: float = 0.15
    # Alertas (Pre-entrega 4)
    alerta_caida_finalizacion_pp: float = 15.0
    alerta_override_rate: float = 0.20
    piso_tasa_finalizacion: float = 0.70
    costo_max_usd_por_sku: float = 0.50


def cargar_umbrales() -> Umbrales:
    """Usa la versión aprobada vigente si existe (ver auto_mejora.py)."""
    path = os.getenv("UMBRALES_PATH")
    if path and Path(path).exists():
        return Umbrales(**json.loads(Path(path).read_text(encoding="utf-8")))
    return Umbrales()


def umbrales_dict(u: Umbrales) -> dict:
    return asdict(u)


# Estados posibles de un SKU: enum fijo, nunca texto libre (Pre-entrega 3).
ESTADOS = (
    "ok",
    "inconsistente",
    "dato_incompleto",
    "sin_fuente_confiable",
    "fuentes_contradictorias",
    "bloqueado_pendiente_aprobacion",
    "corregido",
    "pendiente_aprobacion",
)

ACCIONES = ("corregir_directo", "corregir_y_notificar", "bloquear_pendiente_aprobacion", "ninguna")

# Fuentes que el Agente 2 tiene permitido citar (grounding).
FUENTES_PERMITIDAS = {
    "ficha_proveedor": {"oficial": True},
    "catalogo_fabricante": {"oficial": True},
    "foto_etiqueta": {"oficial": False},
    "historico_correcciones": {"oficial": False},
}

# Modelo: configurable, nunca hardcodeado en la lógica.
MODELO_LLM = os.getenv("LLM_MODEL", "claude-sonnet-5-5")
TEMPERATURA_EXTRACCION = 0.0  # extracción determinística
TEMPERATURA_JURADO = 0.0
