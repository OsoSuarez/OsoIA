#!/usr/bin/env python3
"""Agente de vuelos baratos: Buenos Aires -> Madrid / Roma.

Consulta Google Flights (vía SerpAPI) para:
  - ida y vuelta EZE <-> MAD
  - ida y vuelta EZE <-> FCO
  - multidestino EZE -> MAD ... FCO -> EZE  (y al revés)

Guarda el historial de precios, muestra un ranking y avisa cuando aparece
un precio por debajo del umbral o un nuevo mínimo histórico.

Uso:
    export SERPAPI_KEY=...            # https://serpapi.com (tiene plan gratuito)
    python agente_vuelos.py           # fechas por defecto: 17/07/2027 - 01/08/2027
    python agente_vuelos.py --ida 2027-07-17 --vuelta 2027-08-01 --flex 1 --umbral 1100
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

SERPAPI_URL = "https://serpapi.com/search.json"
ORIGEN = "EZE"
DESTINOS = {"MAD": "Madrid", "FCO": "Roma"}
HISTORIAL = Path(__file__).with_name("historial_precios.json")


# ---------------------------------------------------------------- búsquedas
def construir_busquedas(ida: date, vuelta: date, flex: int) -> list[dict]:
    """Devuelve la lista de consultas (ruta + fechas) a ejecutar."""
    offsets = range(-flex, flex + 1)
    busquedas = []
    for di, dv in itertools.product(offsets, offsets):
        f_ida, f_vuelta = ida + timedelta(days=di), vuelta + timedelta(days=dv)
        if f_vuelta <= f_ida:
            continue
        for dest in DESTINOS:
            busquedas.append({"tipo": "ida_y_vuelta", "ruta": f"{ORIGEN}-{dest}-{ORIGEN}",
                              "ida": f_ida, "vuelta": f_vuelta, "destino": dest})
        for llega, sale in (("MAD", "FCO"), ("FCO", "MAD")):
            busquedas.append({"tipo": "multidestino", "ruta": f"{ORIGEN}-{llega} / {sale}-{ORIGEN}",
                              "ida": f_ida, "vuelta": f_vuelta, "llega": llega, "sale": sale})
    return busquedas


def params_serpapi(b: dict, api_key: str, moneda: str, adultos: int) -> dict:
    p = {"engine": "google_flights", "api_key": api_key, "currency": moneda,
         "hl": "es", "gl": "ar", "adults": adultos}
    if b["tipo"] == "ida_y_vuelta":
        p.update(type=1, departure_id=ORIGEN, arrival_id=b["destino"],
                 outbound_date=b["ida"].isoformat(), return_date=b["vuelta"].isoformat())
    else:
        p.update(type=3, multi_city_json=json.dumps([
            {"departure_id": ORIGEN, "arrival_id": b["llega"], "date": b["ida"].isoformat()},
            {"departure_id": b["sale"], "arrival_id": ORIGEN, "date": b["vuelta"].isoformat()},
        ]))
    return p


def parsear_respuesta(b: dict, data: dict) -> list[dict]:
    """Convierte la respuesta de SerpAPI en una lista de opciones planas."""
    opciones = []
    for grupo in ("best_flights", "other_flights"):
        for o in data.get(grupo, []) or []:
            if not o.get("price"):
                continue
            tramos = o.get("flights", [])
            opciones.append({
                "ruta": b["ruta"], "tipo": b["tipo"],
                "ida": b["ida"].isoformat(), "vuelta": b["vuelta"].isoformat(),
                "precio": o["price"],
                "aerolineas": ", ".join(dict.fromkeys(t.get("airline", "?") for t in tramos)),
                "escalas": max(len(tramos) - 1, 0),
                "duracion_h": round(o.get("total_duration", 0) / 60, 1),
                "via": ", ".join(l.get("id", "") for l in o.get("layovers", []) or []),
                "link": data.get("search_metadata", {}).get("google_flights_url", ""),
            })
    return opciones


def buscar(busquedas, api_key, moneda, adultos, max_escalas) -> tuple[list[dict], list[str]]:
    resultados, errores = [], []
    for b in busquedas:
        try:
            r = requests.get(SERPAPI_URL, params=params_serpapi(b, api_key, moneda, adultos), timeout=60)
            data = r.json()
            if "error" in data:
                raise RuntimeError(data["error"])
            r.raise_for_status()
        except Exception as e:  # una ruta caída no frena al resto
            errores.append(f"{b['ruta']} {b['ida']}→{b['vuelta']}: {e}")
            continue
        resultados += [o for o in parsear_respuesta(b, data) if o["escalas"] <= max_escalas]
    resultados.sort(key=lambda o: o["precio"])
    return resultados, errores


# ---------------------------------------------------------------- historial
def actualizar_historial(resultados: list[dict], moneda: str) -> dict:
    hist = json.loads(HISTORIAL.read_text()) if HISTORIAL.exists() else {"corridas": []}
    minimos_previos = {}
    for c in hist["corridas"]:
        for ruta, precio in c["minimo_por_ruta"].items():
            minimos_previos[ruta] = min(precio, minimos_previos.get(ruta, precio))
    minimo_por_ruta = {}
    for o in resultados:
        minimo_por_ruta.setdefault(o["ruta"], o["precio"])
    hist["corridas"].append({"fecha": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                             "moneda": moneda, "minimo_por_ruta": minimo_por_ruta})
    HISTORIAL.write_text(json.dumps(hist, indent=2, ensure_ascii=False))
    return minimos_previos


# ---------------------------------------------------------------- reporte
def reporte(resultados, errores, minimos_previos, umbral, moneda, top=10) -> tuple[str, bool]:
    lineas = [f"# ✈️ Vuelos Buenos Aires → Madrid / Roma ({moneda})", ""]
    alerta = False
    if not resultados:
        lineas.append("No se encontraron vuelos (¿fechas todavía sin venta o error de API?).")
    else:
        mejor_por_ruta = {}
        for o in resultados:
            mejor_por_ruta.setdefault(o["ruta"], o)
        lineas += ["## Mejor precio por ruta", "",
                   "| Ruta | Fechas | Precio | Aerolíneas | Escalas | Duración | Señal |",
                   "|---|---|---|---|---|---|---|"]
        for ruta, o in mejor_por_ruta.items():
            senal = []
            if umbral and o["precio"] <= umbral:
                senal.append("🔔 bajo umbral"); alerta = True
            prev = minimos_previos.get(ruta)
            if prev is not None and o["precio"] < prev:
                senal.append(f"📉 nuevo mínimo (antes {prev})"); alerta = True
            lineas.append(f"| {ruta} | {o['ida']} → {o['vuelta']} | **{o['precio']}** | {o['aerolineas']} "
                          f"| {o['escalas']} {('(' + o['via'] + ')') if o['via'] else ''} | {o['duracion_h']} h "
                          f"| {' '.join(senal)} |")
        lineas += ["", f"## Top {top} opciones", ""]
        for i, o in enumerate(resultados[:top], 1):
            lineas.append(f"{i}. **{o['precio']} {moneda}** · {o['ruta']} · {o['ida']} → {o['vuelta']} · "
                          f"{o['aerolineas']} · {o['escalas']} escala(s) · [ver]({o['link']})")
    if errores:
        lineas += ["", "## Errores", ""] + [f"- {e}" for e in errores]
    return "\n".join(lineas), alerta


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ida", default=os.getenv("FECHA_IDA", "2027-07-17"))
    ap.add_argument("--vuelta", default=os.getenv("FECHA_VUELTA", "2027-08-01"))
    ap.add_argument("--flex", type=int, default=int(os.getenv("FLEX_DIAS", "0")),
                    help="días de flexibilidad ± en cada fecha (ojo: multiplica las consultas)")
    ap.add_argument("--umbral", type=float, default=float(os.getenv("UMBRAL_PRECIO", "0")),
                    help="avisar si el precio total es menor o igual a este valor")
    ap.add_argument("--moneda", default=os.getenv("MONEDA", "USD"))
    ap.add_argument("--adultos", type=int, default=int(os.getenv("ADULTOS", "1")))
    ap.add_argument("--max-escalas", type=int, default=int(os.getenv("MAX_ESCALAS", "2")))
    ap.add_argument("--salida", help="archivo markdown donde escribir el reporte")
    a = ap.parse_args(argv)

    api_key = os.getenv("SERPAPI_KEY")
    if not api_key:
        print("Falta la variable de entorno SERPAPI_KEY (https://serpapi.com).", file=sys.stderr)
        return 2

    busquedas = construir_busquedas(date.fromisoformat(a.ida), date.fromisoformat(a.vuelta), a.flex)
    print(f"Ejecutando {len(busquedas)} búsquedas…", file=sys.stderr)
    resultados, errores = buscar(busquedas, api_key, a.moneda, a.adultos, a.max_escalas)
    minimos_previos = actualizar_historial(resultados, a.moneda)
    texto, alerta = reporte(resultados, errores, minimos_previos, a.umbral, a.moneda)
    print(texto)
    if a.salida:
        Path(a.salida).write_text(texto)
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"alerta={'true' if alerta else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
