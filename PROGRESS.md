# Progreso — sas-migrator v2

> Actualizado por el loop autónomo. Revisar junto con `PREGUNTAS.md` y `git log --oneline`.

## Estado general

| Etapa | Estado | Detalle |
|---|---|---|
| **0 — Fundaciones** | ✅ Completada | Port del núcleo + fixes + config + CI. **84 tests en verde, ruff limpio.** |
| **1 — Grafo esqueleto** | ✅ Completada | Pipeline 0-8 end-to-end con gates forzados por topología. **90 tests.** |
| **2 — Parser SAS v2 + placement** | ✅ Completada | Tokenizador + parsers dirigidos + placement con evidencia. **113 tests.** |
| **2.5 — Resolución PREGUNTAS Q1-Q3** | ✅ Completada | Vista v2 única (lineage calificado), DB_ENGINES general+config, placement `utility`. **125 tests.** |
| **3 — Human-in-the-loop + MCP** | ✅ Completada | Entrevistas como `interrupt()` con payloads tipados, CLI interactiva, servidor MCP, 5 ADRs. **173 tests.** |
| **4 — Nodos LLM + ensamblador** | ✅ Completada | LLM real en fases 2/3/6 con retry→needs_human, ensamblador determinista, audit placement-aware, eval set. **222 tests (+6 evals live con API key).** |
| **5 — Ejecución, validación e iteración** | ✅ Completada | nbclient tras la pausa sagrada, cascada dialect-agnóstica + diagnóstico LLM, doc-writer, Fase 9 con gate. DoD e2e vs BD de prueba. **249 tests (+6 evals live, +1 LocalDB en CI).** |
| **6 — Golden real + hardening** | ✅ Completada | Trazas LLM, scanner de secretos, fuzz del parser, evals 6→12, harness `.egp` real + nightly, README. El DoD se saldó con dos `.egp` productivos (84 y 175 nodos) — ver "Validación con .egp reales" abajo. |
| **7 — Plan de mejoras integral (P0-P3)** | ✅ Completada | Confiabilidad (P0), completitud de la traducción (P1), optimización (P2) y deuda (P3). **~460 tests unitarios.** Ver sección propia abajo y `docs/ARQUITECTURA.md`. |

## Etapa 7 — Plan de mejoras integral (rama query-log-y-rewind)

Motivado por la corrida real de `Síntesis_M_CR18` (fase 6 bloqueada: 20/75
nodos sin notebook) y una revisión completa del sistema. Además de los
comandos que dan nombre a la rama (rewind, query log del Query Builder,
doctor, reset, backend Foundry, entrevista de a una pregunta, resiliencia a
529/kills), se ejecutó el plan P0-P3:

- **P0 Confiabilidad**: escrituras atómicas únicas (`fsio`), gates que no
  crashean con JSON corrupto y son predicados puros, needs_human idempotente,
  `resume` re-evalúa el gate bloqueado, fase 7 en 3 sub-nodos (interrupt
  aislado + ejecución idempotente por hash), fase 9 reanudable
  (thread `iteration-NNN`), `indexes.build()` sin `SystemExit`, cliente LLM
  honesto (usage fresco, `max_tokens` truncado → NeedsHuman, topes por tarea).
- **P1 Completitud**: el traductor recibe TODO el contexto (datasets del plan,
  conexiones, mejoras M-xxx, review notes, qué deja cada dependencia); system
  en 3 bloques con few-shot y cache escalonado; allowlist declarativa de
  imports (+`output/requirements.txt`); `NodeTranslation` con contrato fuerte
  (`cells` min 1, strategy Literal); retry con memoria y TODAS las fallas;
  chequeos nuevos (`swallowed_exception`, `import_in_cell`, drop-table sin
  falsos positivos de comentarios, escape auditable de loops); split por
  tokenizador; `json_excerpt` (nunca JSON roto al modelo); **verificador LLM**
  (`translation.verify`, sidecar `translation_review.json`, audit medium,
  confianza en `status`); doctor consciente del proveedor.
- **P2 Optimización**: `llm/costs.py` + `max_run_cost_usd` (BudgetExceeded
  reanudable, acumulado del trace), modelo/max_tokens por tarea, caller
  cacheado por workspace, paralelismo opt-in (`max_workers`, notebooks
  byte-idénticos), cache observable (ADR-0010 documenta los descartes:
  Batch API, Send API).
- **P3 Deuda**: loader único de conexiones, fases con fuente única
  (`PHASE_LABELS` + test de biyección), config estricta (clave desconocida =
  error con nombre), deps con piso y techo (cazó el par incompatible
  langgraph-checkpoint 4.x), tests de cascada que cazaron el bug NaN-vs-NaN,
  docs que mentían corregidas. Diferido a conciencia: split mecánico de
  `audit.py`/`egp.py` (ver `docs/ARQUITECTURA.md` §13).

**Documento nuevo**: `docs/ARQUITECTURA.md` — arquitectura exhaustiva con
diagramas (grafo, capa LLM, secuencia de traducción), módulo por módulo.

## Etapa 6 — qué se hizo

- **Trazas LLM locales** (`llm/trace.py`): `TracingCaller` envuelve al caller
  real y registra cada llamada en `state/llm_trace.jsonl` (task, hash del
  prompt, modelo, outcome ok/needs_human/error, intentos, duración, tokens);
  `summarize()` agrega por task. Sin servicios externos; "el traductor anda
  peor" pasa de anécdota a diff de trazas.
- **Hardening como código**:
  - Scanner de secretos en el ensamblador (`secret_detected`): password/pwd
    literal, `sk-ant-`, `AKIA`, bearer tokens — antes era regla de prompt.
  - Fuzzing ligero del parser: 20 casos de SAS malformado/truncado/basura que
    jamás deben crashear `parse_sas_code` ni `classify_placement`.
  - Barrido `datetime.utcnow` deprecado → `datetime.now(UTC)` (15 sitios,
    ~1000 warnings eliminados).
  - `core/db/profile.py` dialect-agnóstico (inspector SQLAlchemy) y con tests
    sobre sqlite — deuda de Etapa 0 saldada.
- **Eval set 6→12 casos recorded**: PROC MEANS/CLASS, TRANSPOSE, first./last.,
  RETAIN acumulado, pushdown de escritura (SELECT INTO), PROC EXPORT CSV.
  Crece hacia 30-50 con bugs reales del `.egp` productivo.
- **Harness `.egp` real** (`tests/integration/`, activado por
  `SASMIG_REAL_EGP`): extracción+parser con backlog de desacuerdos v1-vs-v2,
  pipeline estructural completo en stub, y traducción live nodo a nodo cuyos
  desvíos alimentan el backlog (regla anti-drift: desvío ⇒ verificación
  nueva, nunca más prompt).
- **Nightly** (`.github/workflows/nightly.yml`): suite completa + evals live
  (secret `ANTHROPIC_API_KEY`) + harness `.egp` por `workflow_dispatch` con el
  backlog como artifact.
- **README.md** de operación + ADR-0008 (observabilidad y harness real).

- **Poda post-cierre**: schemas JSON 33→14 (solo los que `check_gate`
  consulta; el resto era doble copia del contrato Pydantic sin consumidor);
  modelos muertos fuera (`NodeClassification`, `ColumnMapping`,
  `PreprocessCandidate` — la clasificación vive como metadata dict en
  `analyze.py`); módulos v1 sin consumidor eliminados
  (`extractors/summarize_flows.py`, `generation_status.py` — la reanudación
  v2 es por checkpointer a granularidad de nodo del grafo, no por script).
- **Validación con .egp reales y poda del cross-check legacy**: dos proyectos
  productivos por el harness — `Síntesis_M_CR18.egp` (84 nodos/18 flujos,
  cazó el bug del BOM) y `sas.egp` (175 nodos/17 flujos, pipeline completo
  done=True). Barrido de ~440 nombres en desacuerdo v1-vs-v2: **cero misses
  del parser v2** — todo eran falsos positivos del regex v1 (SET de SQL como
  SET de DATA step, código comentado, dirección de UPDATE, matrices IML).
  Con esa evidencia se jubiló `_extract_datasets_legacy` y
  `parser_upgrade_report.json`; `enrich_state` queda solo con placement.

**DoD saldado**: el harness corrió con dos `.egp` productivos (ver
"Validación con .egp reales" arriba). El trabajo continuo es hacer crecer el
eval set de 12 hacia 30-50 con los bugs que aparezcan en producción.

## Etapa 5 — qué se hizo

- **Capa DB dialect-agnóstica** (ADR-0007): `db.connection_url` (SQLAlchemy)
  reemplaza la construcción mssql para BD de prueba (sqlite/LocalDB);
  `verify_tables.verify()` in-process con inspector (paso 4 de B4b: tablas
  DESTINO faltantes bloquean — el sistema no crea tablas);
  `cascade.run_cascade()` in-process preservando modos y exits
  (not_applicable=0 / blocked=3 / full=0|1).
- **Ejecución nbclient tras el interrupt `execution_approval`** — la pausa
  sagrada, default recomendado NO ejecutar; `authorize_execution` (MCP) la
  responde; URL de BD por `SASMIG_DB_URL` (bootstrap del ensamblador);
  notebook FAIL bloquea gate 7; notebooks ejecutados conservan outputs.
- **Fase 7 completa**: verify → referencias staged → autorización →
  ejecución → cascada; FAIL/ERROR dispara el **diagnóstico LLM de
  mismatches** (8 patrones `MismatchCause` → `MismatchDiagnosis` en
  `validation_report.diagnoses`). Gate 7 con sustancia (full FAIL bloquea,
  blocked bloquea). `planning` puebla `output_tables` reales (salidas v2
  no-WORK) — la cascada dejó de ser vacua.
- **doc-writer (fase 8)**: `DocsOut` con los 5 documentos en prosa;
  NeedsHuman → template + gate bloqueado.
- **Fase 9 iteración como sub-grafo con gate**: `IterationEntry` obligatoria
  en `iteration_log.json`, re-traducción parcial (traducciones persistidas
  en `state/translations/`), re-audit + validación RE-CORRIDA como condición
  de cierre (WARN honesto sin insumos). `iterate` real en MCP/CLI/sesión.
- **DoD**: e2e completo contra BD de prueba sqlite (B4b confirma BD → verify
  ok → ejecución autorizada escribe → cascada full PASS → iteración cierra
  PASS) + test LocalDB/SQL Server real que corre en CI windows-latest.
- needs_human cubre fases 2/3/6/7/8/9. CI instala extras db y notebook.

## Etapa 4 — qué se hizo

- **`llm/`** (ADR-0006): `StructuredCaller` con structured outputs y retry
  acotado (≤3, correctivos) → `NeedsHuman`; refusal → `NeedsHuman`; transporte
  propaga. `AnthropicCaller` (anthropic lazy — CI no instala el extra;
  modelo pineado `claude-opus-5` por config `llm:`; prompt caching en el
  system estable), `FakeCaller` (valida contra el output_model) y
  `runtime.get_caller/set_caller` (inyección sin pasar por el checkpointer).
- **needs_human**: `state/needs_human.yaml` + bloqueo de gates 2/3/6 por item
  sin resolver — nunca silencio; la traducción fallida además aparece como
  missing_mapping en la auditoría (doble señal).
- **Contratos**: `NodeTranslation` {imports, cells, traceability, confidence,
  warnings} (también output_model del LLM) y `SasPythonMapping` tipado con
  schema + validación en gate 6.
- **Ensamblador determinista** (`core/assembly`): único escritor de
  notebooks; cell_index/cell_count por construcción; chequeos estáticos antes
  de escribir (ast, imports resolubles, to_parquet/duckdb/f-string-SQL,
  strategy_mismatch) — fallo = nodo fuera + needs_human, nunca notebook roto.
  El stub emite NodeTranslation y usa el MISMO ensamblador (CI/golden lo
  cubren). Convención única de rutas `output/...` (fix del bug de cwd en
  generation_status); `gen_run_all` cableado a la fase 6 y exigido por gate.
- **planning por placement**: strategy derivada del placement efectivo
  (clasificador + overrides B4b); utility→python; ambiguous→pandas con
  supuesto visible por nodo.
- **Nodos LLM reales** (rama stub_mode en 2/3/6): análisis map-reduce por PFD
  (reviews únicas + descripciones + fichas M-xxx proposed), matching
  archivo↔nodo (fallback honesto needs_confirmation), traducción por nodo con
  las DOS tablas de patrones (SAS→pandas / SAS SQL→T-SQL) en UN prefijo
  system cacheado.
- **Auditoría placement-aware**: pushdown sin full-table-read+pandas pesado
  (high), pandas sin SQL dinámico (high), hybrid con WHERE (medium), utility
  sin I/O (low). Categoría nueva `placement`.
- **Deriva de origen sin configurar nada**: los valores contra los que compara
  la auditoría se infieren del propio SAS — el host del `URL=` y la tabla que
  el nodo puebla (`extract_http_hosts` / `extract_dest_tables`). Detecta que el
  traductor cambió el endpoint, o que reemplazó la llamada HTTP por un SELECT de
  la tabla que esa llamada debía poblar. `domain_markers`, `sql_engine_markers`
  y `sql_from_markers` salieron de AuditConfig: eran declaración manual de algo
  que está escrito en el código, y sin declararlas las reglas no corrían.
- **Eval set** (`tests/evals/`): 6 casos SAS→propiedades esperadas; modo
  recorded SIEMPRE en CI (harness + estáticos + ensamblador, sin fingir
  modelo) y modo live con `ANTHROPIC_API_KEY`.
- **Fase 7 mínima honesta**: rama no-stub que stage-a referencias SAS
  (`references_staged`); la cascada contra la BD real es Etapa 5.
- Diferido a Etapa 5: cascada de validación vs SQL Server, paso 4 de B4b
  (verify_tables), docs LLM (fase 8 sigue template).

## Etapa 3 — qué se hizo

- **Contratos** (`core/models/interview.py`): `InterviewCard` (payload de UN
  interrupt: tarjeta con preguntas, default recomendado, evidencia, sin
  timestamps) + `CardAnswers` (valor de `Command(resume=...)`) +
  `PlacementDecisions` (B4b). `Question` ganó `recommended_default`/`evidence`.
- **`core/interview/`** (determinista puro, ADR-0001): builders de todas las
  tarjetas (B1-initial fijo; fase 4: mapping, alcance con nativos uno a uno,
  preprocesamiento, ambigüedades, B4b con resolución de placement POR CAUSA
  RAÍZ, M-xxx una a la vez, cierre; aprobación de plan), `validate.py`
  (semántica de respuestas) y `apply.py` (escritores atómicos: la Fase 4
  produce `db_connections.yaml` —primer productor real— y
  `placement_decisions.yaml` con re-clasificación de nodos ambiguos).
- **Nodos `interrupt()`** (`graph/interviews.py`): `ask()` revalida al
  reanudar — inválida ⇒ re-interrupt con `validation_error`, nunca crash;
  rama `stub_mode` intacta (CI y golden de determinismo sin cambios);
  `pending_interrupt` eliminado del estado (LangGraph ya expone el payload).
  Reanudación a mitad de entrevista probada sobre SqliteSaver reconstruyendo
  el grafo (DoD).
- **`service/MigrationSession`** (ADR-0005): única vía al grafo con
  checkpointer; `✅ Fase N completada` derivado del delta de gate_history.
- **CLI**: `run --no-stub` interactiva (render lean: opciones numeradas,
  `(Recomendado)`, Enter = default) o por guion `--answers-file`; `resume`
  continúa entrevistas a mitad; `status` muestra la tarjeta pendiente;
  `serve` levanta el servidor MCP.
- **Servidor MCP** (`mcp_server/server.py`, FastMCP stdio, extra `mcp`):
  `start_migration, status, get_pending_question, answer, approve_plan,
  authorize_execution, iterate` — las dos últimas `not_available` honesto.
- **Snapshots golden** (`tests/golden/`, ADR-0004): payload de cada tarjeta
  byte a byte + invariantes del UX lean; regeneración con `UPDATE_SNAPSHOTS=1`.
- **ADRs 0001-0005** en `docs/adr/` (deuda del principio "toda invariante es
  un ADR" saldada).
- Pendiente diferido a Etapa 5 (anotado): paso 4 de B4b (verificación de
  tablas contra la BD real) — nada de red en el pipeline de la Etapa 3.

## Etapa 2.5 — qué se hizo (respuestas de Seba a PREGUNTAS.md)

- **Q1**: `SASNode.inputs/outputs/libraries` ahora salen de `parse_sas_code`
  (LIB.TABLA calificado); `build_lineage` matchea por nombre calificado con
  fallback corto y tiene tests de contenido (antes solo existencia); el regex
  v1 quedó como `_extract_datasets_legacy` solo para el chequeo cruzado
  (`parser_upgrade_report.json` gana `lost_inputs/lost_outputs` +
  `nodes_with_lost_io` como flag de desacuerdo); `inputs_v2/outputs_v2`
  eliminados del índice (redundantes).
- **Q2**: `DEFAULT_DB_ENGINES` único en `core/parser/statements.py` (~30
  engines SAS/ACCESS + alias) + `resolve_db_engines(workspace)` con
  `parser.extra_db_engines/ignore_db_engines` de project_config.yaml.
- **Q3**: placement `utility` para nodos sin referencias a datos; macro-refs
  sin datos siguen `ambiguous` (B4b).

## Etapa 2 — qué se hizo

- **Tokenizador SAS real** (`core/parser/tokenizer.py`): comentarios `/* */` y `* ;` con semántica SAS correcta (un `* comentario;` solo cuenta como comentario si abre el statement — `monto = a * b;` sobrevive, el bug v1), strings con comillas escapadas, split por statements.
- **Parsers dirigidos** (`core/parser/statements.py`): `DATA a b;` captura ambos outputs, `SET/MERGE x y` ambos inputs, `CREATE TABLE` como output (invisible en v1 — el construct más común de EG), todos los JOIN como inputs, `INSERT/DELETE/UPDATE` como escrituras, LIBNAME de 1 letra, `CONNECT TO ... AS alias` (passthrough), PROC IMPORT/EXPORT, `data=`/`out=` de PROCs, `&lib..tabla` marcado como macro-dependiente en vez de perderse.
- **Clasificador de placement** (`core/parser/placement.py`): `sql_passthrough | sql_pushdown | pandas | hybrid | ambiguous` derivado de la estructura (origen BD/WORK/archivo de cada dataset + lógica DATA step), con evidencia y razones por nodo. Regla acordada: replica la localidad que SAS tenía; librefs sin confirmar → `ambiguous` → entrevista B4b, jamás se adivina.
- **Integración al pipeline**: `enrich_state()` corre en la Fase 2 tras build_indexes; `nodes_index.json` gana `placement/placement_reasons/inputs_v2/outputs_v2/macro_refs`; `parser_upgrade_report.json` compara v1 vs v2 por nodo (sobre el .egp sintético ya recupera el `CREATE TABLE` que v1 perdía — test lo demuestra).
- 23 tests nuevos de parser/placement + 3 de integración. Los campos v1 del extractor NO se tocan (el residuo de extracción sigue intacto); la migración del lineage a la vista v2 queda para la Etapa 4 (ver PREGUNTAS).

## Etapa 1 — qué se hizo

- **Grafo LangGraph** (`graph/builder.py`): las 10 fases encadenadas con un nodo de gate entre cada par; `check_gate()` (el del v1, con sus chequeos de sustancia) es el router de los edges condicionales — **avanzar sin pasar el gate es imposible por topología**, ya no una instrucción. Test que lo prueba: sabotear la fase 1 y verificar que la fase 2 jamás corre.
- **`migration_state.json` lo escribe el runtime** (proyección tipada vía modelo Pydantic en cada gate) — en v1 lo redactaba el LLM a mano y era el único artefacto central sin validar.
- **Stubs LLM deterministas** (`graph/stubs.py`): el pipeline completo corre sin API key. El stub de generación es el embrión del ensamblador: notebooks reales vía nbformat con `cell_index` calculado al ensamblar (el contrato clave de v2) y cell ids fijos por posición.
- **Golden de determinismo**: dos corridas sobre el mismo workspace (con `reset_workspace` entre medio) producen artefactos idénticos módulo timestamps. Cazó 3 fuentes reales de no-determinismo el primer día: contador global de IDs de smells, cell ids aleatorios de nbformat, timestamp en el .md de auditoría.
- **Reanudación**: checkpointer SqliteSaver; test interrumpe antes de la fase 5 y reanuda con `invoke(None)` — continúa sin repetir ni saltar gates.
- **Frontera de arquitectura como test**: `core/` no puede importar langgraph/anthropic/graph/llm.
- **Gate 6 in-process**: la auditoría semántica dejó de ser un subprocess a una ruta de .github/ (rota en v2) y es una llamada de módulo (`core.audit.run_audit`).
- **CLI**: `sas-migrator run|resume|status` (typer) — probada end-to-end sobre workspace real.

## Etapa 0 — qué se hizo

- **Port completo del núcleo determinista** desde `agent-migrator-sas-main` (read-only, intacto): modelos Pydantic, extractor EGP + residuo, gates (`check_gate`), ledger, índices, plan, auditoría, cascada de validación, y los **67 tests originales** — adaptados a imports de paquete (`sas_migrator.core.*`), sin hacks de `sys.path`.
- **Fixes de bugs conocidos del v1**:
  - `reset.py` no borraba nada (resolvía la raíz a `.github/` y reportaba éxito). Reescrito con `--workspace` explícito + tests.
  - `analyze.py` tenía `state` hardcodeado a nivel de módulo; ahora `--state-dir` como el resto.
- **Des-hardcodeo de cliente**: `srvplatdat.bcch.local` y las heurísticas `si3.*`/`df_bd_ctsi` salieron del código. Nuevo `core/config.py` (`project_config.yaml`, ver `project_config.example.yaml`) con defaults neutros; sin servidor configurado → error explícito, nunca un default silencioso. `TrustServerCertificate` ahora es opt-in (antes fijo `yes`).
- **`build_engine` único** en `core/db/engine.py` (antes triplicado en verify_tables/db_profile/run_validation).
- **Schemas dentro del paquete** (`src/sas_migrator/schemas/`) + **test de sincronía modelos↔schemas** (hueco v1: schemas viejos pasaban desapercibidos porque los tests de gates usan fixtures). El test cazó su primer drift real el mismo día.
- CI (GitHub Actions, windows-latest, py3.11/3.12): ruff + pytest. Ruff ya cazó un bug real que los tests no veían (variables sin definir en `db/profile.py` tras el refactor — módulo sin tests, hueco heredado del v1, anotado para Etapa 5).

## Decisiones tomadas (revisables)

- Layout: `core/` (determinista puro, sin imports de LLM/graph) · `graph/` · `llm/` · `mcp_server/` · `cli/`. Un test de arquitectura vigilará esa frontera (Etapa 1).
- Tests en `tests/unit/` en la raíz (no bajo `src/`).
- Versión `2.0.0a0`, paquete sigue llamándose `sas-migrator`.

## Cómo correr

```bash
cd D:\Projects\sas-migrator-v2
.venv\Scripts\python.exe -m pytest tests/ -q --basetemp=.pytest_tmp
```
