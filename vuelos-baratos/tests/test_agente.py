import json
from datetime import date
from unittest import mock

import agente_vuelos as av

RESPUESTA = {
    "search_metadata": {"google_flights_url": "https://www.google.com/travel/flights?x"},
    "best_flights": [{"price": 1150, "total_duration": 900,
                      "flights": [{"airline": "Iberia"}], "layovers": []}],
    "other_flights": [{"price": 980, "total_duration": 1300,
                       "flights": [{"airline": "ITA"}, {"airline": "ITA"}], "layovers": [{"id": "GRU"}]}],
}


def test_construir_busquedas_sin_flex():
    bs = av.construir_busquedas(date(2027, 7, 17), date(2027, 8, 1), 0)
    assert [b["ruta"] for b in bs] == ["EZE-MAD-EZE", "EZE-FCO-EZE", "EZE-MAD / FCO-EZE", "EZE-FCO / MAD-EZE"]


def test_construir_busquedas_con_flex():
    assert len(av.construir_busquedas(date(2027, 7, 17), date(2027, 8, 1), 1)) == 9 * 4


def test_params_multidestino():
    b = av.construir_busquedas(date(2027, 7, 17), date(2027, 8, 1), 0)[2]
    p = av.params_serpapi(b, "k", "USD", 1)
    assert p["type"] == 3
    tramos = json.loads(p["multi_city_json"])
    assert tramos[0]["arrival_id"] == "MAD" and tramos[1]["departure_id"] == "FCO"


def test_busqueda_reporte_e_historial(tmp_path, monkeypatch):
    monkeypatch.setattr(av, "HISTORIAL", tmp_path / "h.json")
    bs = av.construir_busquedas(date(2027, 7, 17), date(2027, 8, 1), 0)
    fake = mock.Mock(json=lambda: RESPUESTA, raise_for_status=lambda: None)
    with mock.patch.object(av.requests, "get", return_value=fake):
        res, err = av.buscar(bs, "k", "USD", 1, max_escalas=2)
    assert not err and res[0]["precio"] == 980 and res[0]["via"] == "GRU"
    previos = av.actualizar_historial(res, "USD")
    assert previos == {}
    texto, alerta = av.reporte(res, err, {"EZE-MAD-EZE": 1200}, umbral=1000, moneda="USD")
    assert alerta and "nuevo mínimo" in texto and "bajo umbral" in texto


def test_filtra_escalas():
    bs = av.construir_busquedas(date(2027, 7, 17), date(2027, 8, 1), 0)[:1]
    fake = mock.Mock(json=lambda: RESPUESTA, raise_for_status=lambda: None)
    with mock.patch.object(av.requests, "get", return_value=fake):
        res, _ = av.buscar(bs, "k", "USD", 1, max_escalas=0)
    assert [o["precio"] for o in res] == [1150]
