# Agente de vuelos baratos: Buenos Aires → Madrid / Roma

Busca todos los días en Google Flights (vía [SerpAPI](https://serpapi.com)) el precio más bajo para el viaje
**17/07/2027 → 01/08/2027** y compara cuatro rutas:

| Ruta | Tipo |
|---|---|
| EZE → MAD → EZE | ida y vuelta |
| EZE → FCO → EZE | ida y vuelta |
| EZE → MAD … FCO → EZE | multidestino (entrás por Madrid, volvés desde Roma) |
| EZE → FCO … MAD → EZE | multidestino (entrás por Roma, volvés desde Madrid) |

Guarda cada corrida en `historial_precios.json` y avisa (abre un issue en GitHub) cuando un precio
queda **bajo el umbral** o marca un **nuevo mínimo histórico** en su ruta.

## Puesta en marcha

1. Creá una cuenta en https://serpapi.com y copiá tu API key (el plan gratuito alcanza para 4 búsquedas por día).
2. En GitHub: *Settings → Secrets and variables → Actions*
   - Secret `SERPAPI_KEY` = tu key.
   - (Opcional) Variable `UMBRAL_PRECIO` = precio máximo en USD que te interesa (por defecto 1100).
3. El workflow `.github/workflows/vuelos-baratos.yml` corre solo todos los días a las ~08:17 (hora Argentina).
   También podés lanzarlo a mano desde *Actions → Run workflow* y elegir días de flexibilidad.

## Uso local

```bash
pip install -r requirements.txt
export SERPAPI_KEY=tu_key
python agente_vuelos.py                       # fechas por defecto
python agente_vuelos.py --flex 1 --umbral 1000 --adultos 2 --max-escalas 1
```

`--flex N` prueba ±N días en ida y vuelta: con `--flex 1` son 36 búsquedas en lugar de 4 (ojo con el cupo de la API).

Tests: `python -m pytest -q tests`
