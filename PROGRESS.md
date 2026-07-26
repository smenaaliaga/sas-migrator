# Progreso — sas-migrator v2

> Actualizado por el loop autónomo. Revisar junto con `PREGUNTAS.md` y `git log --oneline`.

## Estado general

| Etapa | Estado | Detalle |
|---|---|---|
| **0 — Fundaciones** | ✅ Completada | Port del núcleo + fixes + config + CI. **84 tests en verde, ruff limpio.** |
| **1 — Grafo esqueleto** | ✅ Completada | Pipeline 0-8 end-to-end con gates forzados por topología. **90 tests.** |
| **2 — Parser SAS v2 + placement** | ✅ Completada | Tokenizador + parsers dirigidos + placement con evidencia. **113 tests.** |

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
