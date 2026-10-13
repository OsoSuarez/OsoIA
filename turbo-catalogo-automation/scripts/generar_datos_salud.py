"""Set SINTÉTICO de la variante healthcare: catálogo de farmacia.

Mismo flujo que Turbo (catálogo → validación → RAG → decisión → HITL), pero
en un dominio regulado: los campos clínicos (concentración, unidades por
envase, condición de venta, cadena de frío) afectan la seguridad del
paciente, y los reclamos de clientes traen datos personales de salud (PHI).

Todo es inventado: pacientes, DNI, obras sociales, teléfonos y marcas.

Genera:
  data/salud/catalogo_farmacia.csv
  data/salud/prospectos/*.md          fuente oficial (tipo prospecto aprobado)
  data/salud/reclamos/*.json          reclamos con PHI (crudos, acceso restringido)
  data/salud/golden_set_salud.json    estado esperado + criterio de riesgo
  data/salud/revision_humana.csv      lo que decidió el farmacéutico (para la etapa de sombra)

Uso:  python scripts/generar_datos_salud.py
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "salud"

# sku, nombre, subcat, concentracion_mg, unidades_envase, condicion_venta, cadena_frio, peso_kg, L, A, H, rotacion
CATALOGO = [
    ("FAR-001", "Ibuprofeno Alivian 400 mg x 20 comprimidos", "Analgésicos", 4000, 20, "venta_libre", False, 0.04, 9.5, 2.5, 5.0, "alta"),
    ("FAR-002", "Paracetamol Termin 500 mg x 16 comprimidos", "Analgésicos", 500, 16, "venta_libre", False, 0.03, 9.0, 2.0, 5.0, "alta"),
    ("FAR-003", "Amoxicilina Bactox 500 mg x 16 cápsulas", "Antibióticos", 500, 16, "venta_libre", False, 0.04, 10.0, 3.0, 6.0, "media"),
    ("FAR-004", "Insulina NPH Glucem 100 UI/ml frasco 10 ml", "Insulinas", None, 1, "bajo_receta", False, 0.05, 4.0, 4.0, 7.0, "media"),
    ("FAR-005", "Loratadina Alerfin 10 mg x 10 comprimidos", "Antialérgicos", 10, 100, "venta_libre", False, 0.02, 8.0, 1.5, 4.0, "media"),
    ("FAR-006", "Omeprazol Gastrum 20 mg x 14 cápsulas", "Digestivos", 20, 14, "venta_libre", False, 0.05, 9.0, 2.5, 5.0, "media"),
    ("FAR-007", "Ibuprofeno Alivian 600 mg x 10 comprimidos", "Analgésicos", 600, 10, "venta_libre", False, 0.03, 9.5, 2.0, 5.0, "media"),
    ("FAR-008", "Salbutamol Respir aerosol 100 mcg/dosis x 200 dosis", "Respiratorios", 0.1, 200, "bajo_receta", False, 0.04, 4.5, 4.5, 11.0, "media"),
    ("FAR-009", "Diclofenac Inflamex 75 mg x 10 comprimidos", "Antiinflamatorios", 75, 10, "venta_libre", False, 0.03, 9.0, 2.0, 5.0, "media"),
    ("FAR-010", "Clonazepam Calmex 0,5 mg x 30 comprimidos", "Ansiolíticos", 0.5, 30, "bajo_receta", False, 0.02, 8.5, 2.0, 4.5, "baja"),
    ("FAR-011", "Paracetamol Termin 1 g x 8 comprimidos", "Analgésicos", 1000, 8, "venta_libre", False, 0.03, 9.0, 2.0, 5.0, "alta"),
    ("FAR-012", "Cetirizina Alerfin 10 mg x 10 comprimidos", "Antialérgicos", 10, 10, "venta_libre", False, 0.02, 8.0, 1.5, 4.0, "media"),
    ("FAR-013", "Omeprazol Gastrum 20 mg x 28 cápsulas", "Digestivos", 20, 28, "venta_libre", False, 0.04, 9.0, 3.0, 5.0, "media"),
    ("FAR-014", "Enalapril Presin 10 mg x 30 comprimidos", "Cardiovasculares", None, 30, "bajo_receta", False, 0.03, 9.0, 2.0, 5.0, "media"),
    ("FAR-015", "Vacuna antigripal Inmunex jeringa 0,5 ml", "Vacunas", 0, 1, "bajo_receta", True, 0.03, 12.0, 2.0, 3.0, "baja"),
    ("FAR-016", "Omeprazol Gastrum 20 mg x 30 cápsulas", "Digestivos", 20, 30, "venta_libre", False, 0.09, 9.0, 3.0, 5.0, "baja"),
]

# Prospecto aprobado (fuente oficial). Ausente = sin fuente oficial.
PROSPECTOS = {
    "FAR-001": ("400 mg", 20, "Venta libre", "Temperatura ambiente", "0,04 kg"),
    "FAR-002": ("500 mg", 16, "Venta libre", "Temperatura ambiente", "0,03 kg"),
    "FAR-003": ("500 mg", 16, "Venta bajo receta", "Temperatura ambiente", "0,04 kg"),
    "FAR-004": ("100 UI/ml", 1, "Venta bajo receta", "Refrigerar entre 2 y 8 °C. No congelar", "0,05 kg"),
    "FAR-005": ("10 mg", 10, "Venta libre", "Temperatura ambiente", "0,02 kg"),
    "FAR-006": ("20 mg", 14, "Venta libre", "Temperatura ambiente", "0,04 kg"),
    "FAR-007": ("600 mg", 10, "Venta libre", "Temperatura ambiente", "0,03 kg"),
    "FAR-008": ("100 mcg/dosis", 200, "Venta bajo receta", "Temperatura ambiente", "0,04 kg"),
    "FAR-009": ("75 mg", 10, "Venta libre", "Temperatura ambiente", "0,03 kg"),
    "FAR-010": ("0,5 mg", 30, "Venta bajo receta archivada", "Temperatura ambiente", "0,02 kg"),
    "FAR-011": ("1 g", 8, "Venta libre", "Temperatura ambiente", "0,03 kg"),
    "FAR-012": ("10 mg", 10, "Venta libre", "Temperatura ambiente", "0,02 kg"),
    "FAR-013": ("20 mg", 28, "Venta libre", "Temperatura ambiente", "0,04 kg"),
    "FAR-015": ("—", 1, "Venta bajo receta", "Refrigerar entre 2 y 8 °C", "0,03 kg"),
    # FAR-014 y FAR-016 sin prospecto cargado
}

# Reclamos de clientes: traen PHI. Se guardan crudos con acceso restringido;
# al pipeline solo llega la versión redactada.
RECLAMOS = [
    {"id": "REC-101", "sku_id": "FAR-009", "consentimiento_contacto": True,
     "texto": "Paciente: María Gómez, DNI 28.456.789, afiliada OSPLAD 123456789/01, tel 11-5555-1234. "
              "Compré Diclofenac Inflamex para mi tratamiento de artritis y la caja dice 50 mg, no 75 mg como en la app."},
    {"id": "REC-102", "sku_id": "FAR-004", "consentimiento_contacto": False,
     "texto": "Paciente: Jorge Ramírez, DNI 31.222.333, fecha de nacimiento 04/05/1980, diabético tipo 1. "
              "La insulina llegó tibia, sin conservadora. Mail jramirez@correo.com."},
    {"id": "REC-103", "sku_id": "FAR-002", "consentimiento_contacto": True,
     "texto": "Paciente: Ana Torres, CUIL 27-30111222-4. Todo bien con el pedido, solo tardó un poco."},
]

GOLDEN = {
    "FAR-001": {"estado": "escalado_clinico", "riesgo": "alto", "campo": "concentracion_mg", "valor": 400,
                "motivo": "concentración x10 (4000 mg): riesgo de error de dosis"},
    "FAR-003": {"estado": "escalado_clinico", "riesgo": "alto", "campo": "condicion_venta", "valor": "bajo_receta",
                "motivo": "antibiótico publicado como venta libre"},
    "FAR-004": {"estado": "escalado_clinico", "riesgo": "alto", "campo": "cadena_frio", "valor": True,
                "motivo": "insulina sin cadena de frío + reclamo de paciente"},
    "FAR-005": {"estado": "escalado_clinico", "riesgo": "medio", "campo": "unidades_envase", "valor": 10,
                "motivo": "x10 publicado como x100: error de cantidad dispensada"},
    "FAR-006": {"estado": "pendiente_aprobacion", "riesgo": "bajo", "campo": "peso_kg", "valor": 0.04,
                "motivo": "dato logístico con una sola fuente oficial: en salud no alcanza para corregir solo"},
    "FAR-009": {"estado": "escalado_clinico", "riesgo": "medio", "campo": "concentracion_mg",
                "motivo": "reclamo de paciente sobre un campo clínico (el prospecto coincide con el catálogo)"},
    "FAR-010": {"estado": "escalado_clinico", "riesgo": "alto", "campo": "condicion_venta", "valor": "receta_archivada",
                "motivo": "psicotrópico sin receta archivada"},
    "FAR-014": {"estado": "dato_incompleto", "riesgo": "medio", "motivo": "falta la concentración"},
    "FAR-016": {"estado": "pendiente_aprobacion", "riesgo": "bajo", "campo": "peso_kg",
                "motivo": "peso fuera de rango, sin prospecto"},
    # Falsos positivos de ALTO RIESGO: marcarlos y suspenderlos dejaría sin
    # un medicamento esencial a un paciente.
    "FAR-007": {"estado": "ok", "riesgo_fp": "alto", "motivo": "600 mg es correcto aunque difiere del grupo (400 mg)"},
    "FAR-008": {"estado": "ok", "riesgo_fp": "alto", "motivo": "100 mcg = 0,1 mg: broncodilatador de rescate"},
    "FAR-015": {"estado": "ok", "riesgo_fp": "alto", "motivo": "vacuna sin concentración en mg: no aplica"},
}

# Revisión humana de referencia (lo que el farmacéutico hubiera hecho a mano
# en la misma semana): base de comparación de la etapa de sombra.
REVISION_HUMANA = [
    ("FAR-001", "corregir", "concentracion_mg", "400"),
    ("FAR-003", "corregir", "condicion_venta", "bajo_receta"),
    ("FAR-004", "corregir", "cadena_frio", "True"),
    ("FAR-005", "corregir", "unidades_envase", "10"),
    ("FAR-006", "corregir", "peso_kg", "0.04"),
    ("FAR-009", "revisar", "concentracion_mg", "75"),
    ("FAR-010", "corregir", "condicion_venta", "receta_archivada"),
    ("FAR-014", "completar", "concentracion_mg", ""),
    ("FAR-016", "revisar", "peso_kg", ""),
]


def main() -> None:
    (OUT / "prospectos").mkdir(parents=True, exist_ok=True)
    (OUT / "reclamos").mkdir(parents=True, exist_ok=True)
    cols = ["SKU_ID", "PRODUCT_NAME", "SUBCATEGORY_NAME", "CONCENTRACION_MG", "UNIDADES_ENVASE", "CONDICION_VENTA",
            "CADENA_FRIO", "PESO_KG", "LARGO_CM", "ANCHO_CM", "ALTO_CM", "ROTACION"]
    with open(OUT / "catalogo_farmacia.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in CATALOGO:
            w.writerow(["" if v is None else v for v in r])
    for sku, (conc, uds, venta, conserv, peso) in PROSPECTOS.items():
        nombre = next(r[1] for r in CATALOGO if r[0] == sku)
        (OUT / "prospectos" / f"{sku}.md").write_text(
            f"# Prospecto aprobado — {nombre}\n\nCódigo de referencia del cliente: {sku}\n\n"
            f"| Atributo | Valor |\n|---|---|\n| Concentración | {conc} |\n| Unidades por envase | {uds} |\n"
            f"| Condición de venta | {venta} |\n| Conservación | {conserv} |\n| Peso bruto unidad | {peso} |\n",
            encoding="utf-8")
    for rec in RECLAMOS:
        (OUT / "reclamos" / f"{rec['id']}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "golden_set_salud.json").write_text(json.dumps(GOLDEN, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(OUT / "revision_humana.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["SKU_ID", "DECISION", "CAMPO", "VALOR"])
        w.writerows(REVISION_HUMANA)
    print(f"OK: {len(CATALOGO)} SKUs de farmacia, {len(PROSPECTOS)} prospectos, {len(RECLAMOS)} reclamos con PHI, "
          f"{len(GOLDEN)} casos de evaluación")


if __name__ == "__main__":
    main()
