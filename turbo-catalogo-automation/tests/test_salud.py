"""Smoke tests de la variante healthcare."""
import json
from datetime import timedelta
from pathlib import Path

import pytest

from src.hitl import ColaHITL, EscritorCatalogo, ahora
from src.salud import (AuditoriaAccesos, PipelineSalud, concentracion_mg_de, evaluar_salud, guardia_phi,
                       redactar_phi)


@pytest.fixture()
def corrida(tmp_path):
    cola = ColaHITL(tmp_path / "h.db", silenciosa=True)
    pipe = PipelineSalud(cola)
    pipe.escritor.archivo = str(tmp_path / "escrituras.csv")
    salida = pipe.correr(guardar=False, ejecutar_acciones=True)
    return cola, pipe, salida


def test_redaccion_de_phi():
    txt = ("Paciente: María Gómez, DNI 28.456.789, afiliada 123456789/01, tel 11-5555-1234, "
           "mail mg@correo.com, diabética tipo 1, fecha de nacimiento 04/05/1980")
    limpio, tipos = redactar_phi(txt)
    for dato in ("María", "28.456.789", "123456789/01", "5555", "mg@correo.com", "diabética", "04/05/1980"):
        assert dato not in limpio
    assert {"NOMBRE", "DNI", "AFILIADO", "TELEFONO", "EMAIL", "DIAGNOSTICO", "FECHA_NAC"} <= set(tipos)


def test_guardia_phi():
    with pytest.raises(PermissionError):
        guardia_phi("Paciente: Juan Pérez, DNI 30.123.456 reclama por la dosis")
    assert guardia_phi("FAR-001 Ibuprofeno 400 mg x 20") == "FAR-001 Ibuprofeno 400 mg x 20"


def test_unidades_clinicas():
    assert concentracion_mg_de("Salbutamol 100 mcg/dosis") == pytest.approx(0.1)
    assert concentracion_mg_de("Paracetamol 1 g x 8") == 1000
    assert concentracion_mg_de("Insulina 100 UI/ml") is None


def test_auditoria_accesos(tmp_path):
    aud = AuditoriaAccesos(tmp_path / "a.db")
    with pytest.raises(PermissionError):
        aud.leer_reclamo_crudo("REC-101", "alguien", "analista_catalogo", "curiosidad")
    rec = aud.leer_reclamo_crudo("REC-101", "Laura", "farmaceutico", "evaluar reclamo de dosis")
    assert rec["sku_id"] == "FAR-009"
    resultados = [r[-1] for r in aud.registros()]
    assert resultados == ["denegado", "permitido"]  # el intento denegado también queda


def test_golden_salud(tmp_path):
    rep = evaluar_salud(tmp_path)
    assert rep["exactitud_estado"] >= 0.95
    assert rep["recall_alto_riesgo"] == 1.0
    assert rep["falsos_positivos_alto_riesgo"] == 0
    assert rep["fugas_phi"] == 0
    assert rep["autocorrecciones_clinicas"] == 0


def test_escritor_rechaza_campo_clinico(tmp_path):
    esc = EscritorCatalogo(str(tmp_path / "x.csv"))
    with pytest.raises(PermissionError):
        esc.corregir("FAR-001", "concentracion_mg", 4000, 400, fuente="agente", version="salud-v1")


def test_salud_doble_firma(corrida):
    cola, _, _ = corrida
    with pytest.raises(PermissionError):  # el dominio salud no acepta roles comerciales
        cola.decidir("FAR-001", "concentracion_mg", "aprobado", "Marina", "analista_catalogo", "es 400 mg", 400)
    r1 = cola.decidir("FAR-001", "concentracion_mg", "aprobado", "Laura", "farmaceutico", "prospecto: 400 mg", 400)
    assert r1["resultado"] == "primera_firma" and r1["valor_aplicado"] is None
    with pytest.raises(PermissionError):  # la misma persona no puede firmar dos veces
        cola.decidir("FAR-001", "concentracion_mg", "aprobado", "Laura", "director_tecnico", "ok", 400)
    r2 = cola.decidir("FAR-001", "concentracion_mg", "aprobado", "Diego", "director_tecnico", "verificado", 400)
    assert r2["valor_aplicado"] == 400


def test_politica_espera_salud(corrida):
    cola, _, _ = corrida
    esc = cola.revisar_vencimientos(ahora() + timedelta(hours=3))
    assert {"FAR-001", "FAR-003", "FAR-004", "FAR-010"} <= set(esc)  # riesgo alto: 2 h
    assert "FAR-009" not in esc  # riesgo medio: 24 h


def test_retencion_solo_en_alto_riesgo(corrida):
    _, _, salida = corrida
    retenidos = {r["sku_id"] for r in salida["resultados"] if "retener_dispensa" in r["acciones_automaticas"]}
    assert retenidos == {"FAR-001", "FAR-003", "FAR-004", "FAR-010"}
    assert salida["estados"]["FAR-008"] == "ok"  # broncodilatador de rescate: no se toca
