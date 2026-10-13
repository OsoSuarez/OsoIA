# Variante healthcare: mapeo, controles reforzados, threat model y matriz de roles

El flujo de Turbo controla datos **comerciales** (peso y medidas de productos). Para la variante healthcare se aplica el mismo flujo a un **catálogo de farmacia** (`src/salud.py`, datos en `data/salud/`). Ahí aparecen dos cosas que el caso comercial no tiene:

- **Campos clínicos.** Concentración, unidades por envase, condición de venta y cadena de frío. Un error en estos campos afecta la seguridad del paciente.
- **PHI** (información de salud identificable). Llega en los reclamos de pacientes: nombre, DNI, obra social, diagnóstico.

## 1. Mapeo del caso a healthcare

| Pieza del flujo | Catálogo Turbo (comercial) | Catálogo de farmacia (salud) |
|---|---|---|
| Dato que se controla | peso, dimensiones, contenido neto | + concentración, unidades por envase, condición de venta, cadena de frío |
| Fuente oficial | ficha del proveedor, catálogo del fabricante | prospecto aprobado (equivalente a la autoridad sanitaria) |
| Evidencia no oficial | foto del rótulo, histórico | reclamos de pacientes (con PHI), foto del rótulo |
| Dato sensible | comercial (precio, condiciones de pago) | **PHI**: nombre, DNI/CUIL, afiliado, teléfono, email, fecha de nacimiento, diagnóstico |
| Error de alto impacto | bloquear un SKU que vende bien | dosis x10, antibiótico o psicotrópico sin receta, insulina sin cadena de frío |
| Falso positivo de alto riesgo | sacar de venta un pack legítimo | dejar sin un medicamento esencial a un paciente (broncodilatador de rescate, vacuna) |
| Quién decide | analista / líder de Catálogo | farmacéutico / director técnico |
| Validación clínica | no aplica | todo hallazgo clínico lo revisa un farmacéutico; riesgo alto → doble firma |

## 2. Controles: qué alcanza para datos comerciales y qué se endurece en salud

| Control | Suficiente para datos comerciales | Endurecido para salud (implementado en `src/salud.py` / `src/hitl.py`) |
|---|---|---|
| Corrección automática | confianza alta + desvío ≤ 30% + no alta rotación | **nunca** en campos clínicos: el escritor rechaza la escritura sin firma humana (defensa en profundidad) |
| Fuentes para corregir logística sola | 1 fuente oficial | **2 fuentes oficiales** que coincidan |
| Revisor | analista de Catálogo | farmacéutico; riesgo alto → **doble firma** (otra persona, una de ellas director técnico) |
| Acción ante riesgo alto | proponer bloqueo; nunca vence (protege la venta) | **retención de dispensa automática y reversible**: el SKU sigue publicado y cada pedido pasa por el farmacéutico antes de salir |
| Política de espera | 48 h → escala al líder | riesgo alto **2 h**, medio 24 h → escala al director técnico |
| Reclamo de cliente | evidencia no oficial | si menciona un dato clínico, **siempre** va al farmacéutico, aunque el dato coincida |
| Datos personales | no hay en el flujo | **redacción de PHI antes** de indexar, loguear, encolar o enviar a un modelo |
| Salida a servicios externos | chunk físico o imagen del rótulo | `guardia_phi`: si el texto todavía contiene PHI, no sale (`PermissionError`) |
| Lectura del dato crudo | no aplica | solo roles habilitados, con propósito declarado; **cada acceso se audita**, permitido o denegado |
| Consentimiento | no aplica | el reclamo se usa para seguridad del paciente (base legítima); sin consentimiento de contacto, el farmacéutico ve "no contactar al paciente" |
| Retención de logs | 90 días | logs sin PHI 90 días; PHI cruda en almacén separado y cifrado (ver matriz) |
| Evaluación | exactitud, precisión, recall, fidelidad | + recall de alto riesgo = 100%, falsos positivos de alto riesgo = 0, fugas de PHI = 0, autocorrecciones clínicas = 0 |

## 3. Threat model específico para PHI (STRIDE)

| # | Amenaza | Vector en este flujo | Control | Dónde se verifica |
|---|---|---|---|---|
| S | Suplantación de un revisor para aprobar un cambio clínico | llamada a `POST /hitl/{sku}/decision` con un rol falso | roles por dominio + doble firma de dos personas distintas; en producción el rol sale del token SSO, no del body | `test_salud_doble_firma` |
| T | Manipulación de un prospecto o reclamo para inducir un cambio (prompt injection) | texto con instrucciones dentro de un documento | el modelo solo devuelve JSON con esquema y no tiene herramientas que actúen; el estado lo calcula código; un campo clínico nunca se escribe sin firma | `test_escritor_rechaza_campo_clinico` |
| R | Repudio: "yo no aprobé ese cambio" | disputa interna o con un proveedor | `registro_aprobaciones` con quién, rol, cuándo, fuente, justificación; doble firma registrada por separado | registro en SQLite |
| I | **Divulgación de PHI**: al proveedor del modelo | prompt con el reclamo crudo | redacción previa + `guardia_phi` en `llm.llamar_json(verificar_phi=True)` | `test_guardia_phi` |
| I | **Divulgación de PHI**: en logs, base vectorial o cola humana | indexar o loguear el texto original | solo se indexa y loguea el texto redactado; la eval busca cada fragmento de PHI en base vectorial, cola, logs y salida | `fugas_phi = 0` en `python -m src.salud` |
| I | **Divulgación de PHI**: por lectura interna no autorizada | un rol sin necesidad lee el reclamo crudo | `AuditoriaAccesos`: roles habilitados + propósito obligatorio; los intentos denegados también quedan registrados | `test_auditoria_accesos` |
| I | **Divulgación de PHI**: en el repositorio | datos reales subidos al repo público | datos 100% sintéticos; `.env` fuera del repo | revisión de PR |
| D | Denegación de servicio que deja un hallazgo clínico sin revisar | cola saturada o farmacéutico ausente | SLA de 2 h y escalamiento al director técnico; retención de dispensa mientras tanto | `test_politica_espera_salud` |
| E | Elevación de privilegios del agente | el agente intenta corregir un campo clínico o despublicar | la escritura clínica no existe sin `aprobado_por_humano=True`; la única acción automática es la retención (restrictiva y reversible) | `test_escritor_rechaza_campo_clinico` |

**Riesgos residuales** (quedan fuera del prototipo y se resuelven antes de producción):
- la redacción por regex no detecta nombres sin el prefijo "Paciente:" ni texto libre muy desestructurado: en producción se agrega un modelo NER local, con el regex como segunda capa;
- cifrado en reposo del almacén de reclamos crudos;
- contrato con el proveedor del modelo sin retención de datos;
- validación con el DPO y Legal del marco aplicable. En Argentina, la Ley 25.326 trata los datos de salud como sensibles; si hubiera pacientes de otras jurisdicciones aplicarían otros marcos (por ejemplo, HIPAA o GDPR).

## 4. Matriz de roles, permisos, retención y auditoría

| Rol | Puede leer | Puede decidir | No puede | Auditoría |
|---|---|---|---|---|
| `servicio_redaccion` (pipeline) | reclamo crudo, solo para redactar | — | indexar o enviar texto sin redactar | cada lectura en `auditoria_accesos` |
| Agente 2 (pipeline) | prospectos, reclamos **redactados**, catálogo | proponer; retener dispensa (riesgo alto) | escribir campos clínicos; despublicar; leer PHI cruda | log por SKU con `trace_id` |
| `analista_catalogo` | cola comercial | decisiones del dominio comercial | decidir hallazgos clínicos (403) | `registro_aprobaciones` |
| `lider_catalogo` | cola comercial | bloqueos de alta rotación | decidir hallazgos clínicos (403) | `registro_aprobaciones` |
| `farmaceutico` | cola clínica; reclamo crudo con propósito | aprobar o rechazar hallazgos clínicos (primera firma en riesgo alto) | segunda firma de su propia decisión | `registro_aprobaciones` + `auditoria_accesos` |
| `director_tecnico` | igual que farmacéutico | segunda firma obligatoria en riesgo alto; recibe los escalamientos | — | ídem |
| `dpo` | auditoría de accesos; reclamo crudo con propósito | — | decidir sobre el catálogo | `auditoria_accesos` |

| Dato | Dónde vive | Retención | Quién accede |
|---|---|---|---|
| Reclamo crudo (PHI) | almacén separado (en el repo: `data/salud/reclamos/`, sintético) | lo mínimo para resolver el reclamo; propuesta: 180 días | servicio de redacción, farmacéutico, director técnico, DPO |
| Reclamo redactado | base vectorial | mientras esté vigente el reclamo | pipeline |
| Logs de operación | `logs/pipeline.jsonl` (sin PHI) | 90 días | equipo técnico |
| Registro de aprobaciones | SQLite → Postgres | 24 meses (trazabilidad sanitaria) | líder, director técnico, auditoría |
| Auditoría de accesos | `auditoria_accesos_salud.db` | 24 meses | DPO, seguridad |

## 5. Golden set healthcare (`data/salud/golden_set_salud.json`)

16 SKUs de farmacia y 3 reclamos con PHI. Cubre los cuatro tipos de caso que pidió el feedback:

| Tipo de caso | SKUs | Qué se espera |
|---|---|---|
| Datos sensibles | reclamos REC-101/102/103 (nombre, DNI, CUIL, afiliado, teléfono, email, fecha de nacimiento, diagnóstico) | 0 fragmentos de PHI en base vectorial, cola, logs y salida |
| Incertidumbre | FAR-009: el reclamo del paciente dice 50 mg, el prospecto y el catálogo dicen 75 mg | escalado clínico de riesgo medio (no se corrige, lo mira un farmacéutico) |
| Escalamiento clínico | FAR-001 (dosis x10), FAR-003 (antibiótico como venta libre), FAR-004 (insulina sin cadena de frío), FAR-010 (psicotrópico sin receta archivada), FAR-005 (x10 publicado como x100) | escalado clínico; riesgo alto → retención de dispensa + doble firma |
| Falsos positivos de alto riesgo | FAR-007 (600 mg, distinto del grupo), FAR-008 (100 mcg = 0,1 mg), FAR-015 (vacuna sin concentración en mg) | quedan **ok**: no se marca ni se retiene un medicamento esencial |

Correr: `python -m src.salud`. Imprime las métricas y la compuerta (sale con código 1 si falla).

## 6. Etapa de sombra (`python -m src.sombra --dominio comercial|salud`)

Corre el flujo sin escribir nada y lo compara con lo que la persona revisó a mano (`revision_humana.csv`). El reporte separa tres cosas:
- **desempeño técnico:** precisión y recall frente al humano, concordancia del valor propuesto;
- **seguridad:** escrituras que el humano no haría, alto riesgo que se escapa, falsos positivos de alto riesgo, fugas de PHI;
- **carga de revisión humana.**

Los resultados de la corrida sobre los datos sintéticos están en `reports/sombra_*.json` y en la sección 8 del documento. Con datos reales se usa el mismo comando, pasando el export de la semana y las decisiones reales del analista o del farmacéutico (ver `docs/reproducibilidad.md`).
