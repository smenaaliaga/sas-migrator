# sas-migrator v2

Migrador de proyectos SAS Enterprise Guide (`.egp`) a Python (notebooks
pandas/SQL), orquestado con LangGraph. Reingeniería del v1 con tres reglas
duras:

1. **Los gates son topología, no disciplina**: la fase N+1 no puede correr si
   el gate N no pasó — es imposible por construcción del grafo.
2. **El LLM nunca escribe artefactos**: propone contenido estructurado
   (Pydantic); los notebooks y JSONs los escribe código determinista.
3. **Nunca silencio**: un trabajo LLM sin output utilizable termina en la cola
   `needs_human`, que bloquea el gate hasta resolverse a mano.

## Instalación

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev,graph,cli,mcp,db,notebook,llm]"
```

El extra `llm` (SDK de Anthropic) solo hace falta para corridas reales; los
tests y el modo stub no lo usan.

## Estructura del workspace

```
mi_migracion/
├── project_config.yaml      # copiar de project_config.example.yaml
├── input/
│   ├── egp/proyecto.egp     # el .egp a migrar
│   ├── data/*.csv           # referencias SAS (ground truth de la cascada)
│   └── docs/                # documentación de negocio opcional
├── state/                   # artefactos por fase (los escribe el runtime)
└── output/                  # notebooks generados
```

## Uso

### CLI

```bash
# Corrida completa (entrevistas interactivas en terminal)
sas-migrator run mi_migracion --no-stub

# Corrida determinista sin LLM ni entrevistas (CI, smoke test)
sas-migrator run mi_migracion

# Reanudar donde quedó (checkpointer sqlite)
sas-migrator resume mi_migracion

# Estado de fases y gates
sas-migrator status mi_migracion

# Iteración post-migración (Fase 9)
sas-migrator iterate mi_migracion "corregir el redondeo de montos" --nodes CodeTask-3
```

### Chat (MCP)

```bash
sas-migrator serve mi_migracion   # servidor MCP por stdio
```

Tools: `start_migration`, `status`, `get_pending_question`, `answer`,
`approve_plan`, `authorize_execution`, `iterate`. La ejecución de notebooks
**siempre** exige autorización explícita (default recomendado: NO ejecutar).

## Fases y gates

| Fase | Qué hace | Gate bloquea si |
|---|---|---|
| 0 Intake | Valida workspace y config | falta estructura |
| 1 Entrevista inicial | Contexto de negocio (tarjetas B1) | respuestas requeridas sin contestar |
| 2 Análisis SAS | Extracción .egp + parser v2 + revisión LLM nodo a nodo | residuo sin explicar, nodos sin revisar, needs_human |
| 3 Profiling + matching | Perfila datos y los cruza con datasets SAS | needs_human |
| 4 Entrevista post-análisis | B1–B6: alcance, BD (B4b), mejoras M-xxx | decisiones faltantes, needs_human |
| 5 Plan de traducción | Plan por nodo + aprobación explícita | `user_approved != true` |
| 6 Generación | Traducción LLM + **ensamblador determinista** | chequeos estáticos, needs_human |
| 7 Validación | verify BD → **pausa sagrada** → nbclient → cascada vs referencia | notebook FAIL, mismatch, needs_human |
| 8 Documentación | README + mapping SAS→Python | artefactos faltantes |
| 9 Post-migración | Iteraciones con su propio sub-grafo y gate | validación no re-corrida |

## Tests

```bash
# Suite rápida (CI en cada push): unit + golden + evals recorded
.venv\Scripts\python.exe -m pytest tests/ -q --basetemp=.pytest_tmp

# Evals live (mismos asserts contra el LLM real)
ANTHROPIC_API_KEY=... pytest tests/evals -q

# Harness contra un .egp real (integración; escribe backlog JSON)
SASMIG_REAL_EGP=C:/ruta/proyecto.egp pytest tests/integration -q
```

Tres velocidades: la suite rápida corre en CI por push (`ci.yml`); las evals
live y el harness real corren en el nightly (`nightly.yml`) o a mano.

- Regenerar schemas tras tocar modelos:
  `PYTHONIOENCODING=utf-8 python -m sas_migrator.core.utils.generate_schemas`
- Regenerar snapshots golden intencionalmente: `UPDATE_SNAPSHOTS=1 pytest tests/golden`

## Observabilidad

Cada llamada LLM real queda en `state/llm_trace.jsonl` (task, hash del prompt,
outcome, intentos, duración, tokens). Resumen rápido:

```python
from sas_migrator.llm.trace import summarize
summarize(Path("mi_migracion/state"))
```

## Documentos

- `PROGRESS.md` — bitácora por etapa.
- `PREGUNTAS.md` — decisiones abiertas/resueltas.
- `docs/adr/` — decisiones de arquitectura (frontera core, interrupts,
  ensamblador, ejecución, observabilidad).
