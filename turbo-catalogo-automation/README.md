# Turbo — Control automático de medidas y peso del catálogo

Flujo de automatización con IA que detecta, investiga y corrige errores de **peso y dimensiones** en el catálogo de Turbo Argentina, con aprobación humana para las decisiones de alto impacto.

Proyecto final integrador del curso **AI Automation Avanzado** (Coderhouse). El documento técnico completo (arquitectura, decisiones, métricas y plan de producción) está linkeado en la entrega; este repo es el artefacto reproducible.

> **Datos:** todo lo que hay en `data/` es **sintético**. Replica los patrones de error encontrados en una corrida real contra Snowflake (unidad en g vs kg, packs "N x M g" ambiguos, contenido neto en 0, peso del bulto cargado como unidad, dimensión en otra unidad), con marcas ficticias. El nombre de la tabla real y las credenciales viven solo en `.env`.

## Qué hace

```
evento de catálogo (webhook) ─┐
corrida semanal (scheduler) ──┴─> Capa 1: unidades + rango físico ──> dato_incompleto (carga manual)
                                      │
                                      ▼
                         Agente 1: rango IQR por subcategoría + validador de unidades
                                      │ cola priorizada (alta rotación primero)
                                      ▼
   fichas PDF ─ LlamaCloud ─> Markdown ─> chunks ─> Chroma ──┐
   catálogo del fabricante ──────────────────────────────────┤ búsqueda híbrida (RAG)
   foto del rótulo ─ modelo de visión ───────────────────────┤ multimodal
   histórico de correcciones ────────────────────────────────┘
                                      ▼
                         Agente 2: evidencia ─> confianza ─> validación de salida
                           │                         │
                corregir_directo               HITL: aprobar / rechazar / pedir evidencia
                (alta + desvío≤30% +           (bloquear un SKU activo SIEMPRE es humano)
                 no alta rotación)                   │
                           └────────> catálogo (dry-run o PATCH) + registro auditable
                                      ▼
                logs JSON · métricas Prometheus · alertas · modo seguro · evals · auto-mejora
```

| Requisito de la consigna | Dónde está |
|---|---|
| APIs REST y webhooks | `src/api.py` (webhook firmado HMAC, idempotente, 202 + background), `src/hitl.py` (PATCH idempotente al catálogo, webhook saliente a Slack) |
| Fallbacks y políticas de espera | `src/resiliencia.py` (backoff + jitter, circuit breaker), fallbacks en `datos.py`, `parsing.py`, `rag.py`, `vision.py`, `hitl.py`; timeouts HITL en `hitl.py` |
| Logs, métricas, observabilidad | `src/observabilidad.py` (JSON con `trace_id`, p50/p95, `/metrics`), alertas y modo seguro en `pipeline.py` |
| RAG con base vectorial | `src/parsing.py` + `src/rag.py` (Chroma, chunking por producto, búsqueda híbrida, grounding por identidad) |
| Multimodal | `src/vision.py` (foto del rótulo → JSON con esquema obligatorio) |
| Human-in-the-Loop | `src/hitl.py` (cola, roles, registro de aprobaciones, escalamiento) |
| Evaluaciones automáticas | `src/evals.py` (golden set, precisión/recall, jurado de fidelidad, compuerta de despliegue) |
| Workflow auto-mejorable | `src/auto_mejora.py` (overrides → umbrales candidatos → evaluación en sombra → aprobación del líder) |
| LlamaCloud / NotebookLM | `src/parsing.py` (LlamaParse); `data/parsed/*.md` son las fuentes para cargar en NotebookLM |

## Correr en 3 minutos (sin credenciales)

```bash
cd turbo-catalogo-automation
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python scripts/generar_datos_muestra.py   # regenera los datos sintéticos (ya vienen en el repo)
pytest -q                                 # 14 smoke tests
python run_demo.py --reset                # corrida + decisiones humanas + timeouts + auto-mejora + falla de Snowflake
python -m src.evals                       # evaluación offline con compuerta (exit 1 si no pasa)
uvicorn src.api:app --reload              # API en http://127.0.0.1:8000/docs
```

Notebook de experimentación (ejecutado, con salidas): [`notebooks/01_experimentacion.ipynb`](notebooks/01_experimentacion.ipynb).

### Probar el webhook a mano

(Linux/macOS; en Windows guardá el bloque de Python en un archivo y corrélo.)

```bash
python - <<'EOF'
import json, httpx
from src.api import firmar
cuerpo = json.dumps({"event_id": "ev-001", "tipo": "sku.updated", "sku_id": "GAL-007"}).encode()
print(httpx.post("http://127.0.0.1:8000/webhooks/catalogo", content=cuerpo,
                 headers={"X-Signature": firmar(cuerpo), "Content-Type": "application/json"}).json())
EOF
curl http://127.0.0.1:8000/hitl/pendientes
curl http://127.0.0.1:8000/metrics
```

## Modo online

Copiá `.env.example` a `.env` y completá lo que tengas. Cada integración es opcional y tiene su ruta alternativa:

| Variable | Activa | Si falta o falla |
|---|---|---|
| `ANTHROPIC_API_KEY` | visión sobre el rótulo, jurado LLM, extracción de fichas con layout raro | respuestas grabadas / jurado por reglas / regex |
| `LLAMA_CLOUD_API_KEY` (+ `pip install llama-cloud-services`) | parsing con LlamaParse | pypdf |
| `SNOWFLAKE_*` + `CATALOGO_TABLA` | catálogo real | CSV de muestra |
| `SLACK_WEBHOOK_URL` | notificaciones HITL | `reports/notificaciones.log` |
| `DRY_RUN=0` + `CATALOGO_API_URL` + `CATALOGO_API_TOKEN` | escritura real (PATCH) | escribe en `reports/correcciones_aplicadas.csv` |

## Resultados de la demo (datos sintéticos, 59 SKUs)

| Métrica | v1 | v2 (propuesta por auto-mejora) |
|---|---|---|
| Exactitud de estado vs. golden set | 98,3% | 100% |
| Precisión del detector | 91,7% | 100% |
| Recall del detector | 100% | 100% |
| Exactitud del valor propuesto | 100% | 100% |
| Fidelidad a la fuente (jurado) | 100% | 100% |

El único error de v1 es un pack legítimo (`BAR-006`, 3x25 g) comparado contra barritas sueltas. El sistema propone bloquearlo, el analista lo rechaza con justificación, y ese rechazo genera la propuesta v2 (`separar_packs=True`), que la evaluación en sombra confirma.

## Estructura

```
src/            código del pipeline (ver tabla de arriba)
scripts/        generador de datos sintéticos
data/           catálogo, fichas PDF, specs de fabricante, fotos, golden set, fichas parseadas
notebooks/      experimentación paso a paso
tests/          smoke tests (pytest)
docs/           diagramas (Mermaid)
.github/        CI: tests + compuerta de evaluación en cada push
```
