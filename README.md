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

### Credenciales

Qué backend se usa lo decide `project_config.yaml` → `llm.provider`; la
credencial viene del entorno, nunca de la config. Copiar `.env.example` a
`.env` y completar **solo** el bloque del proveedor que corresponda:

| `llm.provider` | Variables |
|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` |
| `foundry` | `ANTHROPIC_FOUNDRY_API_KEY` + `ANTHROPIC_FOUNDRY_RESOURCE` (o `llm.foundry_resource` en la config, que no es secreto) |

Precedencia: entorno del proceso > `<workspace>/.env` > `.env` de la raíz. Una
corrida real además necesita `db.default_server` en la config (lo usa la fase 7
para `verify_tables`) y referencias en `input/data/`.

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

Hay tres frentes y **uno solo** ejecuta: CLI y MCP son clientes delgados de
`MigrationSession`, la misma sesión in-process. Elegir frente no cambia lo que
corre, solo cómo se contestan las entrevistas.

### CLI

`--workspace` es una opción, no un posicional; sin ella se usa el directorio
actual.

```bash
# Corrida completa (entrevistas interactivas en terminal)
sas-migrator run --workspace mi_migracion --no-stub

# Corrida determinista sin LLM ni entrevistas (CI, smoke test)
sas-migrator run --workspace mi_migracion

# Reanudar donde quedó, incluida una entrevista a medio contestar
sas-migrator resume --workspace mi_migracion

# Rehacer una fase DESDE CERO (resume la continúa; rewind la reinicia)
sas-migrator rewind --phase 6 --workspace mi_migracion

# Estado de fases y gates
sas-migrator status --workspace mi_migracion

# Iteración post-migración (Fase 9)
sas-migrator iterate --workspace mi_migracion \
  --describe "corregir el redondeo de montos" --nodes CodeTask-3
```

`resume` vs `rewind`: las respuestas de entrevista viven en el checkpointer, no
en `state/`. `resume` retoma donde iba; `rewind --phase N` descarta el tramo y
vuelve a preguntar desde el inicio de la fase N (las fases anteriores no se
recalculan: sus artefactos en `state/` quedan). `rewind` respalda el checkpoint
en `.bak` salvo `--no-backup`.

Cualquier corrida no interactiva (CI, replay de una migración) acepta un guion
YAML de respuestas — con `default: recommended`, las tarjetas no listadas toman
el camino recomendado:

```bash
sas-migrator run --workspace mi_migracion --no-stub --answers-file respuestas.yaml
```

```yaml
default: recommended
answers:
  B2-scope:queries:                    # las consultas de inspección, todas juntas
    Q-B2-4-Query-abc123: "Excluir de la migración"
  B1-initial:
    Q-001: "Síntesis mensual de cuentas nacionales"
```

### Chat (MCP)

```bash
sas-migrator serve --workspace mi_migracion   # servidor MCP por stdio
```

Un servidor por workspace. Tools: `start_migration`, `status`,
`get_pending_question`, `answer`, `approve_plan`, `authorize_execution`,
`iterate`. La ejecución de notebooks **siempre** exige autorización explícita
(default recomendado: NO ejecutar).

Es el frente que conviene para la entrevista post-análisis: las tarjetas llegan
como JSON tipado (con el SQL de cada consulta en `context`) y se contestan en
lenguaje natural en vez de tipear números. Para registrarlo en un host MCP:

```json
{
  "mcpServers": {
    "sas-migrator": {
      "command": "D:/Projects/sas-migrator-v2/.venv/Scripts/sas-migrator.exe",
      "args": ["serve", "--workspace", "D:/Migraciones/mi_proyecto"]
    }
  }
}
```

`answer` toma la tarjeta entera de una: `card_id` más una lista
`[{question_id, value}]`. Una tarjeta que agrupa N nodos —las consultas de
Enterprise Guide— se responde en **una** llamada con N pares, no en N llamadas.

### Python in-process

Lo que envuelven los otros dos. Sirve para scriptear una corrida entera o
inspeccionar el estado sin levantar nada:

```python
from pathlib import Path
from sas_migrator.service import MigrationSession

s = MigrationSession(Path("mi_migracion"))
result = s.start(stub_mode=False)          # .resume() / .rewind_to_phase(6)
while (card := s.pending()) is not None:
    result = s.answer({
        "card_id": card.card_id,
        "answers": [{"question_id": q.id, "value": q.recommended_default}
                    for q in card.questions],
        "free_text": "",
    })
print(result.status, result.phase, result.gate_errors)
```

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
