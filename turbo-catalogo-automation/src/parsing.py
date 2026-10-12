"""Parsing de fichas técnicas (PDF) a Markdown.

Ruta primaria: LlamaCloud (LlamaParse, salida markdown). Reconstruye tablas
y títulos de PDFs con layout complejo.
Ruta alternativa (sin LLAMA_CLOUD_API_KEY o si el servicio falla): pypdf +
reconstrucción de la tabla etiqueta/valor. Peor con layouts raros, pero
alcanza para las fichas estándar y no frena la corrida.

Después de parsear, SIEMPRE se descartan los campos comerciales (precio,
condiciones de pago, descuentos): el agente solo puede ver especificación
física (grounding, Pre-entrega 3). Se filtra antes de indexar, así el dato
confidencial nunca llega a la base vectorial ni al prompt.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from pypdf import PdfReader

from .config import DATA_DIR
from .observabilidad import METRICAS, log
from .resiliencia import ErrorPermanente, ErrorTransitorio, con_fallback, reintentar

PARSED_DIR = DATA_DIR / "parsed"
CAMPOS_CONFIDENCIALES = re.compile(r"precio|condiciones de pago|descuento|costo|bonificaci", re.I)
ETIQUETAS = ["Código cliente", "Contenido neto", "Peso bruto unidad", "Dimensiones unidad (L x A x H)",
             "Precio de lista", "Condiciones de pago"]


@reintentar(intentos=3, base_s=2.0, max_s=20.0, servicio="llamacloud")
def _llamaparse(pdf: Path) -> str:
    if not os.getenv("LLAMA_CLOUD_API_KEY"):
        raise ErrorPermanente("sin LLAMA_CLOUD_API_KEY")  # no tiene sentido reintentar
    try:
        from llama_cloud_services import LlamaParse
    except ImportError as e:
        raise ErrorPermanente("llama-cloud-services no instalado") from e
    parser = LlamaParse(result_type="markdown", language="es", verbose=False)
    try:
        docs = parser.load_data(str(pdf))
    except Exception as e:  # noqa: BLE001
        raise ErrorTransitorio(str(e)) from e
    return "\n\n".join(d.text for d in docs)


def _pypdf_a_markdown(pdf: Path) -> str:
    lineas = [l.strip() for p in PdfReader(str(pdf)).pages for l in (p.extract_text() or "").splitlines()]
    lineas = [l for l in lineas if l]
    md, i = [], 0
    while i < len(lineas):
        l = lineas[i]
        if l.startswith("Ficha técnica"):
            titulo = l + (" " + lineas[i + 1] if i + 1 < len(lineas) and lineas[i + 1].startswith("(") else "")
            md.append(f"# {titulo}")
            i += 2 if titulo != l else 1
            continue
        if l.startswith("Producto "):
            md += ["", f"## {l}", "", "| Atributo | Valor |", "|---|---|"]
        elif l in ETIQUETAS and i + 1 < len(lineas):
            md.append(f"| {l} | {lineas[i + 1]} |")
            i += 1
        else:
            md.append(l)
        i += 1
    return "\n".join(md)


def filtrar_confidencial(md: str) -> tuple[str, int]:
    limpias, n = [], 0
    for l in md.splitlines():
        if CAMPOS_CONFIDENCIALES.search(l):
            n += 1
            continue
        limpias.append(l)
    return "\n".join(limpias), n


def parsear_fichas(carpeta: Path | None = None) -> list[Path]:
    carpeta = carpeta or DATA_DIR / "fuentes" / "fichas_proveedor"
    PARSED_DIR.mkdir(parents=True, exist_ok=True)
    salidas = []
    for pdf in sorted(carpeta.glob("*.pdf")):
        with METRICAS.cronometro("parsing_pdf"):
            md, ruta = con_fallback(lambda: _llamaparse(pdf), lambda: _pypdf_a_markdown(pdf), "parsing")
        md, descartadas = filtrar_confidencial(md)
        destino = PARSED_DIR / f"{pdf.stem}.md"
        destino.write_text(f"<!-- fuente: {pdf.name} | parser: {'llamacloud' if ruta == 'primaria' else 'pypdf'} -->\n"
                           + md, encoding="utf-8")
        METRICAS.inc("pdfs_parseados")
        log("INFO", "ficha parseada", archivo=pdf.name, parser="llamacloud" if ruta == "primaria" else "pypdf",
            lineas_confidenciales_descartadas=descartadas)
        salidas.append(destino)
    return salidas
