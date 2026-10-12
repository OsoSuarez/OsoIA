"""Genera el set de datos SINTÉTICO del repo.

Por qué sintético: el repo es público y el catálogo real de Turbo es dato
interno. Los casos replican los patrones encontrados en la corrida real
contra Snowflake (muestra de 541 SKUs): unidad en gramos vs kilos, packs
"N x M g" ambiguos, contenido neto en 0 o 1, peso del bulto cargado como
unidad, dimensión en otra unidad. Las marcas son ficticias.

Genera:
  data/catalogo_muestra.csv          catálogo (lo que hoy está en Snowflake)
  data/fuentes/fichas_proveedor/*.pdf  fichas técnicas (input del parsing)
  data/fuentes/catalogo_fabricante/*.md especificaciones del fabricante
  data/fuentes/historico_correcciones.csv
  data/fotos/*.png                    rótulos logísticos (input multimodal)
  data/fotos/lecturas_grabadas.json   respuestas grabadas del modelo de visión
  data/eval/golden_set.json           resultado esperado por SKU (evals)

Uso:  python scripts/generar_datos_muestra.py
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# (sku, nombre, subcategoría, net_qty, unidad, peso_kg, largo, alto, ancho, rotación, proveedor)
# None = campo vacío en el catálogo.
CATALOGO = [
    # --- Gaseosas 1,5 L ---
    ("GSE-001", "Gaseosa Cola Andina 1,5 L", "Gaseosas 1,5 L", 1.5, "L", 1550, 9.5, 31.0, 9.5, "alta", "PRV-BEB"),
    ("GSE-002", "Gaseosa Lima Andina 1,5 L", "Gaseosas 1,5 L", 1.5, "L", 1.58, 9.5, 31.0, 9.5, "media", "PRV-BEB"),
    ("GSE-003", "Gaseosa Naranja Andina 1,5 L", "Gaseosas 1,5 L", 1.5, "L", 1.57, 9.5, 31.0, 9.5, "media", "PRV-BEB"),
    ("GSE-004", "Gaseosa Pomelo Andina 1,5 L", "Gaseosas 1,5 L", 1.5, "L", 1.59, 9.5, 31.2, 9.5, "baja", "PRV-BEB"),
    ("GSE-005", "Gaseosa Cola Zero Andina 1,5 L", "Gaseosas 1,5 L", 1.5, "L", 1.56, 9.5, 31.0, 9.5, "alta", "PRV-BEB"),
    ("GSE-006", "Gaseosa Tónica Brisa 1,5 L", "Gaseosas 1,5 L", 1.5, "L", 1.61, 9.6, 30.5, 9.6, "baja", "PRV-BEB"),
    ("GSE-007", "Gaseosa Cola Brisa 1,5 L", "Gaseosas 1,5 L", 1.5, "L", 1.6, 9.6, 30.5, 9.6, "media", "PRV-BEB"),
    ("GSE-008", "Gaseosa Limón Brisa 1,5 L", "Gaseosas 1,5 L", 1.5, "L", 1.59, 9.6, 30.6, 9.6, "media", "PRV-BEB"),
    ("GSE-009", "Gaseosa Manzana Brisa 1,5 L", "Gaseosas 1,5 L", 1.5, "L", 6.4, 9.6, 30.6, 9.6, "media", "PRV-BEB"),
    # --- Yogur bebible 1 kg ---
    ("YOG-001", "Yogur Bebible Frutilla Pampa 1 kg", "Yogur bebible 1 kg", 1, "KG", 1.05, 8.5, 23.5, 8.5, "alta", "PRV-LAC"),
    ("YOG-002", "Yogur Bebible Vainilla Pampa 1 kg", "Yogur bebible 1 kg", 1, "KG", 1.05, 8.5, 23.5, 8.5, "media", "PRV-LAC"),
    ("YOG-003", "Yogur Bebible Durazno Pampa 1 kg", "Yogur bebible 1 kg", 1, "KG", 1.06, 8.5, 23.4, 8.5, "media", "PRV-LAC"),
    ("YOG-004", "Yogur Bebible Natural Pampa 1 kg", "Yogur bebible 1 kg", 1, "KG", 10.3, 8.5, 23.5, 8.5, "baja", "PRV-LAC"),
    ("YOG-005", "Yogur Bebible Frutilla Sur 1 kg", "Yogur bebible 1 kg", 1, "KG", 1.04, 8.6, 23.0, 8.6, "media", "PRV-LAC"),
    ("YOG-006", "Yogur Bebible Vainilla Sur 1 kg", "Yogur bebible 1 kg", 1, "KG", 1.04, 8.6, 2.35, 8.6, "baja", "PRV-LAC"),
    ("YOG-007", "Yogur Bebible Banana Sur 1 kg", "Yogur bebible 1 kg", 1, "KG", 1.05, 8.6, 23.0, 8.6, "baja", "PRV-LAC"),
    ("YOG-008", "Yogur Bebible Descremado Sur 1 kg", "Yogur bebible 1 kg", 1, "KG", 1.04, 8.6, 23.1, 8.6, "media", "PRV-LAC"),
    # --- Galletitas dulces 300 g ---
    ("GAL-001", "Galletitas Chocolate Molino 300 g", "Galletitas 300 g", 300, "G", 0.32, 20.0, 5.5, 7.0, "alta", "PRV-SNK"),
    ("GAL-002", "Galletitas Vainilla Molino 300 g", "Galletitas 300 g", 300, "G", 0.32, 20.0, 5.5, 7.0, "media", "PRV-SNK"),
    ("GAL-003", "Galletitas Coco Molino 300 g", "Galletitas 300 g", 300, "G", 0.33, 20.0, 5.6, 7.0, "media", "PRV-SNK"),
    ("GAL-004", "Galletitas Limón Molino 300 g", "Galletitas 300 g", 300, "G", 0.31, 20.0, 5.5, 7.0, "baja", "PRV-SNK"),
    ("GAL-005", "Galletitas Rellenas Trigal 300 g", "Galletitas 300 g", 300, "G", 0.33, 19.5, 5.8, 7.2, "media", "PRV-SNK"),
    ("GAL-006", "Galletitas Avena Trigal 300 g", "Galletitas 300 g", 300, "G", 0.32, 19.5, 5.8, 7.2, "media", "PRV-SNK"),
    ("GAL-007", "Galletitas Avena Miel Trigal 300 g", "Galletitas 300 g", 300, "G", 0.40, 19.5, 5.8, 7.2, "baja", "PRV-SNK"),
    ("GAL-008", "Galletitas Integrales Trigal 300 g", "Galletitas 300 g", 300, "G", 0.32, 19.5, 5.7, 7.2, "baja", "PRV-SNK"),
    ("GAL-009", "Galletitas Queso Kesis 3x96 g", "Galletitas 300 g", 95, "G", 0.31, 19.0, 6.0, 7.0, "media", "PRV-SNK"),
    # --- Papas fritas 150 g ---
    ("SNK-001", "Papas Fritas Clásicas Crocante 150 g", "Papas fritas 150 g", 150, "G", 0.17, 30.0, 22.0, 8.0, "alta", "PRV-SNK"),
    ("SNK-002", "Papas Fritas Jamón Crocante 150 g", "Papas fritas 150 g", 150, "G", 0.17, 30.0, 22.0, 8.0, "media", "PRV-SNK"),
    ("SNK-003", "Papas Fritas Pollo Crocante 150 g", "Papas fritas 150 g", 150, "G", 0.21, 30.0, 22.0, 8.0, "alta", "PRV-SNK"),
    ("SNK-004", "Papas Fritas Queso Crocante 150 g", "Papas fritas 150 g", 150, "G", 0.17, 30.0, 22.0, 8.0, "media", "PRV-SNK"),
    ("SNK-005", "Papas Fritas Onduladas Ruta 150 g", "Papas fritas 150 g", 150, "G", 0.21, 30.5, 21.5, 8.5, "baja", "PRV-SNK2"),
    ("SNK-006", "Papas Fritas Pimienta Ruta 150 g", "Papas fritas 150 g", 150, "G", 0.17, 30.5, 21.5, 8.5, "media", "PRV-SNK2"),
    ("SNK-007", "Papas Fritas Ketchup Ruta 150 g", "Papas fritas 150 g", 150, "G", 0.18, 30.5, 21.5, 8.5, "baja", "PRV-SNK2"),
    ("SNK-008", "Papas Fritas Cheddar Ruta 150 g", "Papas fritas 150 g", 150, "G", 0.17, 30.5, None, 8.5, "media", "PRV-SNK2"),
    ("SNK-009", "Papas Fritas Provenzal Ruta 150 g", "Papas fritas 150 g", 150, "G", 0.17, 30.5, 21.5, 8.5, "media", "PRV-SNK2"),
    ("SNK-010", "Papas Fritas Barbacoa Crocante 150 g", "Papas fritas 150 g", 150, "G", 0.17, 30.0, 22.0, 8.0, "alta", "PRV-SNK"),
    ("SNK-011", "Papas Fritas Sal Marina Crocante 150 g", "Papas fritas 150 g", 150, "G", 0.18, 30.0, 22.0, 8.0, "media", "PRV-SNK"),
    ("SNK-012", "Papas Fritas Lisas Ruta 150 g", "Papas fritas 150 g", 150, "G", 0.17, 30.5, 21.5, 8.5, "baja", "PRV-SNK2"),
    # --- Quesos untables 300 g ---
    ("QUE-001", "Queso Untable Clásico Pampa 300 g", "Quesos untables 300 g", 300, "G", 0.33, 11.0, 6.0, 11.0, "alta", "PRV-LAC"),
    ("QUE-002", "Queso Untable Light Pampa 300 g", "Quesos untables 300 g", 300, "G", 0.33, 11.0, 6.0, 11.0, "media", "PRV-LAC"),
    ("QUE-003", "Queso Untable Finas Hierbas Pampa 300 g", "Quesos untables 300 g", 300, "G", 0.34, 11.0, 6.0, 11.0, "baja", "PRV-LAC"),
    ("QUE-004", "Queso Untable Clásico Sur 300 g", "Quesos untables 300 g", 300, "G", 0.32, 10.8, 6.2, 10.8, "media", "PRV-LAC"),
    ("QUE-005", "Queso Untable Ciboulette Sur 300 g", "Quesos untables 300 g", 300, "G", 0.55, 10.8, 6.2, 10.8, "media", "PRV-LAC"),
    ("QUE-006", "Queso Untable Light Sur 300 g", "Quesos untables 300 g", 300, "G", 0.33, 10.8, 6.2, 10.8, "baja", "PRV-LAC"),
    # --- Cortes de res ---
    ("RES-001", "Bife de Chorizo Estancia 500 g", "Res", 0.5, "KG", 0.53, 25.0, 4.0, 18.0, "alta", "PRV-CAR"),
    ("RES-002", "Vacío Estancia 500 g", "Res", 0.5, "KG", 0.54, 25.0, 4.0, 18.0, "alta", "PRV-CAR"),
    ("RES-003", "Cuadril Estancia 500 g", "Res", 0.5, "KG", 0.53, 25.0, 4.0, 18.0, "media", "PRV-CAR"),
    ("RES-004", "Entraña Estancia 500 g", "Res", 500, "G", 0.53, 25.0, 4.0, 18.0, "media", "PRV-CAR"),
    ("RES-005", "Matambre Estancia 500 g", "Res", 0.5, "KG", 0.54, 25.0, 4.2, 18.0, "baja", "PRV-CAR"),
    # --- Postres ---
    ("POS-001", "Tiramisú Dolce 6x70 g", "Postres refrigerados", 420, "G", 0.47, 18.0, 7.0, 12.0, "media", "PRV-LAC"),
    ("POS-002", "Flan Casero Dolce 4x100 g", "Postres refrigerados", 400, "G", 0.45, 18.0, 7.0, 12.0, "media", "PRV-LAC"),
    ("POS-003", "Mousse Chocolate Dolce 4x100 g", "Postres refrigerados", 400, "G", 0.45, 18.0, 7.0, 12.0, "baja", "PRV-LAC"),
    ("POS-004", "Arroz con Leche Dolce 4x110 g", "Postres refrigerados", 440, "G", 0.48, 18.0, 7.2, 12.0, "baja", "PRV-LAC"),
    # --- Barritas de cereal ---
    ("BAR-001", "Barrita Cereal Brava Frutilla 16 g", "Barritas de cereal", 0, "G", 0.02, 11.0, 1.7, 3.0, "media", "PRV-SNK"),
    ("BAR-002", "Barrita Cereal Brava Chocolate 25 g", "Barritas de cereal", 1, "G", 0.03, 11.5, 1.8, 3.2, "baja", "PRV-SNK"),
    ("BAR-003", "Barrita Cereal Granja Miel 25 g", "Barritas de cereal", 25, "G", 0.03, 11.5, 1.8, 3.2, "media", "PRV-SNK"),
    ("BAR-004", "Barrita Cereal Granja Coco 25 g", "Barritas de cereal", 25, "G", 0.03, 11.5, 1.8, 3.2, "baja", "PRV-SNK"),
    ("BAR-005", "Barrita Cereal Granja Maní 25 g", "Barritas de cereal", 25, "G", 0.03, 11.5, 1.8, 3.2, "media", "PRV-SNK"),
    ("BAR-006", "Pack Barritas Granja 3x25 g", "Barritas de cereal", 75, "G", 0.085, 12.0, 4.5, 3.5, "baja", "PRV-SNK"),
]

# Fichas técnicas de proveedor: un PDF por proveedor con una sección por SKU.
# Incluyen campos comerciales confidenciales que el parser debe DESCARTAR.
FICHAS = {
    "lacteos_del_sur.pdf": ("Lácteos del Sur S.A. (PRV-LAC)", [
        ("YOG-004", "Yogur Bebible Natural Pampa 1 kg", "1 kg", "1,05 kg", "8,5 x 8,5 x 23,5 cm", "$ 1.850", "60 días fecha factura"),
        ("YOG-006", "Yogur Bebible Vainilla Sur 1 kg", "1 kg", "1,04 kg", "8,6 x 8,6 x 23,5 cm", "$ 1.790", "60 días fecha factura"),
        ("QUE-005", "Queso Untable Ciboulette Sur 300 g", "300 g", "0,33 kg", "10,8 x 10,8 x 6,2 cm", "$ 2.400", "45 días"),
        ("QUE-004", "Queso Untable Clásico Sur 300 g", "300 g", "0,32 kg", "10,8 x 10,8 x 6,2 cm", "$ 2.300", "45 días"),
        ("POS-001", "Tiramisú Dolce 6x70 g", "420 g (6 x 70 g)", "0,47 kg", "18 x 12 x 7 cm", "$ 4.100", "30 días"),
    ]),
    "snacks_pampa.pdf": ("Snacks Pampa S.R.L. (PRV-SNK)", [
        ("GAL-007", "Galletitas Avena Miel Trigal 300 g", "300 g", "0,32 kg", "19,5 x 7,2 x 5,8 cm", "$ 980", "30 días"),
        ("SNK-003", "Papas Fritas Pollo Crocante 150 g", "150 g", "0,17 kg", "30 x 8 x 22 cm", "$ 1.450", "30 días"),
        ("BAR-001", "Barrita Cereal Brava Frutilla 16 g", "16 g", "0,02 kg", "11 x 3 x 1,7 cm", "$ 350", "30 días"),
        ("GAL-001", "Galletitas Chocolate Molino 300 g", "300 g", "0,32 kg", "20 x 7 x 5,5 cm", "$ 990", "30 días"),
    ]),
}

FABRICANTE = {
    "GAL-007": ("Trigal Alimentos", "Galletitas Avena Miel 300 g", "0,32 kg", "19,5 x 7,2 x 5,8 cm"),
    "YOG-004": ("Pampa Lácteos", "Yogur Bebible Natural 1 kg", "1,05 kg", "8,5 x 8,5 x 23,5 cm"),
    # Contradice a la ficha del proveedor (0,33 kg): caso fuentes_contradictorias.
    "QUE-005": ("Sur Quesos", "Queso Untable Ciboulette 300 g (formato 500 g)", "0,55 kg", "12 x 12 x 7 cm"),
}

HISTORICO = [
    # sku, fecha, campo, valor_anterior, valor_corregido, fuente
    ("SNK-005", "2026-03-14", "peso_kg", 0.30, 0.17, "remedicion_fisica"),
    ("GAL-003", "2026-05-02", "alto_cm", 55.0, 5.6, "ficha_proveedor"),
]

# Rótulos logísticos fotografiados (input multimodal).
FOTOS = {
    "GAL-007": ("TRIGAL - GALLETITAS AVENA MIEL", "PESO BRUTO 0,32 kg", "19,5 x 7,2 x 5,8 cm"),
    "SNK-005": ("RUTA SNACKS - PAPAS ONDULADAS 150 g", "PESO BRUTO 0,17 kg", "30,5 x 8,5 x 21,5 cm"),
    "QUE-005": ("SUR - QUESO UNTABLE CIBOULETTE 300 g", "PESO BRUTO 0,33 kg", "10,8 x 10,8 x 6,2 cm"),
    "SNK-003": ("CROCANTE - PAPAS POLLO 150 g", "PESO BRUTO 0,17 kg", "30 x 8 x 22 cm"),
}

GOLDEN = {
    "GSE-001": {"estado": "dato_incompleto", "motivo": "peso fuera de rango físico (gramos cargados como kg)"},
    "GSE-009": {"estado": "bloqueado_pendiente_aprobacion", "motivo": "peso del bulto cargado como unidad, sin fuente"},
    "YOG-004": {"estado": "pendiente_aprobacion", "campo": "peso_kg", "valor": 1.05},
    "YOG-006": {"estado": "pendiente_aprobacion", "campo": "alto_cm", "valor": 23.5},
    "GAL-007": {"estado": "corregido", "campo": "peso_kg", "valor": 0.32},
    "SNK-003": {"estado": "pendiente_aprobacion", "campo": "peso_kg", "valor": 0.17, "motivo": "alta rotación"},
    "SNK-005": {"estado": "pendiente_aprobacion", "campo": "peso_kg", "valor": 0.17, "motivo": "confianza media"},
    "SNK-008": {"estado": "dato_incompleto", "motivo": "falta alto_cm"},
    "QUE-005": {"estado": "fuentes_contradictorias"},
    "BAR-001": {"estado": "pendiente_aprobacion", "campo": "net_quantity", "valor": 16},
    "BAR-002": {"estado": "sin_fuente_confiable", "campo": "net_quantity"},
    # El dato es CORRECTO (pack 3x25 g legítimo dentro de 'Barritas de cereal').
    # Con umbrales v1 el detector lo marca y propone bloquearlo: falso positivo
    # que ataja la aprobación humana. La recalibración v2 (separar packs) lo corrige.
    "BAR-006": {"estado": "ok", "motivo": "pack 3x25 g legítimo; v1 lo marca (falso positivo)"},
    # Casos que NO deben marcarse (falsos positivos históricos):
    "RES-004": {"estado": "ok", "motivo": "500 G = 0,5 KG tras estandarizar unidad"},
    "POS-001": {"estado": "ok", "motivo": "6x70 g = 420 g"},
    "GAL-009": {"estado": "ok", "motivo": "3x96 g con neto unitario: lectura ambigua aceptada"},
}


def escribir_catalogo() -> None:
    cols = ["SKU_ID", "PRODUCT_NAME", "SUBCATEGORY_NAME", "NET_QUANTITY", "MEASUREMENT_UNIT",
            "PESO_KG", "LARGO_CM", "ALTO_CM", "ANCHO_CM", "ROTACION", "PROVEEDOR_ID", "ACTIVO"]
    with open(DATA / "catalogo_muestra.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in CATALOGO:
            w.writerow([("" if v is None else v) for v in r] + [True])


def escribir_fichas() -> None:
    out = DATA / "fuentes" / "fichas_proveedor"
    out.mkdir(parents=True, exist_ok=True)
    st = getSampleStyleSheet()
    for archivo, (proveedor, items) in FICHAS.items():
        doc = SimpleDocTemplate(str(out / archivo), pagesize=A4)
        elems = [Paragraph(f"Ficha técnica de productos — {proveedor}", st["Title"]),
                 Paragraph("Documento para clientes mayoristas. Versión 2026-09.", st["Normal"]), Spacer(1, 12)]
        for sku, nombre, neto, peso, dims, precio, pago in items:
            elems.append(Paragraph(f"Producto {sku} — {nombre}", st["Heading2"]))
            elems.append(Table([
                ["Código cliente", sku],
                ["Contenido neto", neto],
                ["Peso bruto unidad", peso],
                ["Dimensiones unidad (L x A x H)", dims],
                ["Precio de lista", precio],
                ["Condiciones de pago", pago],
            ]))
            elems.append(Spacer(1, 10))
        doc.build(elems)


def escribir_fabricante() -> None:
    out = DATA / "fuentes" / "catalogo_fabricante"
    out.mkdir(parents=True, exist_ok=True)
    for sku, (fab, nombre, peso, dims) in FABRICANTE.items():
        (out / f"{sku}.md").write_text(
            f"# {fab} — Especificación de producto\n\n"
            f"Producto: {nombre}\nCódigo de referencia del cliente: {sku}\n\n"
            f"| Atributo | Valor |\n|---|---|\n| Peso bruto unidad | {peso} |\n"
            f"| Dimensiones unidad (L x A x H) | {dims} |\n",
            encoding="utf-8",
        )


def escribir_historico() -> None:
    p = DATA / "fuentes" / "historico_correcciones.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["SKU_ID", "FECHA", "CAMPO", "VALOR_ANTERIOR", "VALOR_CORREGIDO", "FUENTE"])
        w.writerows(HISTORICO)


def _fuente(tam: int):
    for nombre in ("DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(nombre, tam)
        except OSError:
            continue
    return ImageFont.load_default()


def escribir_fotos() -> None:
    out = DATA / "fotos"
    out.mkdir(parents=True, exist_ok=True)
    grabadas = {}
    for sku, (titulo, peso, dims) in FOTOS.items():
        img = Image.new("RGB", (900, 520), (238, 230, 214))  # cartón
        d = ImageDraw.Draw(img)
        d.rectangle([40, 40, 860, 480], fill=(255, 255, 255), outline=(20, 20, 20), width=4)
        d.text((70, 70), titulo, fill=(0, 0, 0), font=_fuente(30))
        d.text((70, 170), peso, fill=(0, 0, 0), font=_fuente(48))
        d.text((70, 260), f"DIM. LxAxH {dims}", fill=(0, 0, 0), font=_fuente(34))
        d.text((70, 360), f"REF. CLIENTE {sku}   LOTE 2609A", fill=(60, 60, 60), font=_fuente(26))
        for i in range(0, 300, 6):  # código de barras decorativo
            d.rectangle([520 + i, 400, 522 + i + (i % 3), 460], fill=(0, 0, 0))
        img = img.rotate(2, expand=False, fillcolor=(238, 230, 214))  # foto levemente torcida
        img.save(out / f"{sku}.png")
        peso_num = float(peso.split()[2].replace(",", "."))
        grabadas[sku] = {"peso_kg": peso_num, "dimensiones_cm": dims, "legible": True,
                         "texto_detectado": f"{titulo} | {peso} | DIM. LxAxH {dims}"}
    (out / "lecturas_grabadas.json").write_text(json.dumps(grabadas, indent=2, ensure_ascii=False), encoding="utf-8")


def escribir_golden() -> None:
    p = DATA / "eval"
    p.mkdir(parents=True, exist_ok=True)
    (p / "golden_set.json").write_text(json.dumps(GOLDEN, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    DATA.mkdir(exist_ok=True)
    escribir_catalogo()
    escribir_fichas()
    escribir_fabricante()
    escribir_historico()
    escribir_fotos()
    escribir_golden()
    print(f"OK: {len(CATALOGO)} SKUs, {sum(len(v[1]) for v in FICHAS.values())} fichas, "
          f"{len(FABRICANTE)} specs de fabricante, {len(FOTOS)} fotos, {len(GOLDEN)} casos de evaluación")
