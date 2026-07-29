# ADR-0006 — Nodos LLM con retry acotado; el ensamblador es determinista

**Estado**: aceptada (Etapa 4)

## Decisión

1. **El LLM vive en `llm/`** (frontera: `core/` no lo importa jamás). El
   contrato es `StructuredCaller.call(...) -> BaseModel` con structured
   outputs: retry acotado (≤ `llm.max_validation_retries`, default 3) con
   mensajes correctivos; agotado o refusal ⇒ `NeedsHuman`. Los errores de
   transporte propagan (visibles, reanudables por checkpointer). El modelo va
   pineado por config (`llm.model`, default `claude-opus-5`); sin parámetros
   de sampling (removidos del API); prompt caching en los bloques system
   estables (las tablas de patrones), lo volátil en el mensaje user.
2. **`NeedsHuman` nunca es silencio**: queda en `state/needs_human.yaml`
   (id NH-NNN, fase, task, nodo, razón) y el gate de la fase (2/3/6) bloquea
   por cada item sin resolver. El nodo de traducción que falla un chequeo
   estático SÍ entra al mapping, marcado `degraded: true` (ver §3), así que la
   segunda señal ya no es `missing_mapping` sino un issue `degraded` de
   severidad high que emite la auditoría por cada entrada marcada. El gate
   bloquea igual (`issues_by_severity.high > 0`) sin código nuevo. Un nodo sin
   traducción —que nunca llegó a existir— sigue cayendo en `missing_mapping`.
3. **El ensamblador (`core/assembly`) es determinista puro** y es el ÚNICO
   escritor de notebooks: el traductor (LLM o stub) emite `NodeTranslation`
   (celdas sin headers ni anclas) y el ensamblador construye el template,
   calcula `cell_index`/`cell_count` desde los índices reales y escribe
   `sas_python_mapping.json` correcto por construcción. Chequeos estáticos
   ANTES de escribir (`ast.parse`, imports resolubles, `to_parquet`/`duckdb`/
   f-string-SQL prohibidos, strategy_mismatch, undefined_name…): un fallo
   **marca** el nodo, no lo descarta. Se emite igual, precedido de una celda
   markdown con el chequeo fallido, la confianza del traductor, el veredicto
   del verificador y sus supuestos, y con `degraded: true` en el mapping. Solo
   `syntax_error` y `empty_translation` no se pueden emitir tal cual: ahí van
   el mismo banner, el código original en markdown y una celda que levanta
   `NotImplementedError`. El camino stub usa el MISMO ensamblador — CI y el
   golden de determinismo lo cubren siempre.

   *Enmienda (2026-07-29).* Hasta acá la regla era la inversa: *"un fallo omite
   el nodo (needs_human), jamás un notebook roto"*. El trade-off resultó al
   revés de lo esperado. En `Síntesis_M_CR18` una respuesta ambigua de la
   entrevista dejó sin definir `engine`, y los 55 nodos que lo usaban cayeron
   por `undefined_name`: 8 h 53 min y ~$64 de traducciones correctas terminaron
   en 14 notebooks, 7 de ellos cascarones de título + imports. Nada de eso se
   veía al abrir el `.ipynb`. Un notebook que puede romper al ejecutar es
   preferible a uno silenciosamente incompleto, porque lo primero se ve y lo
   segundo no. La propiedad que sí se conserva: el gate sigue bloqueando, y el
   trabajo dudoso queda a la vista del revisor en vez de perdido.
4. **`stub_mode` gobierna un solo eje**: LLM stub vs real (fases 2/3/6) y
   entrevistas stub vs reales (fases 1/4/5) van juntos. La corrida real es el
   default de los tres frentes y `--stub` la excepción de CI (al escribirse
   este ADR la CLI usaba el flag inverso, `--no-stub`, y era la única de los
   tres que arrancaba en stub); los tests inyectan `FakeCaller` vía
   `llm.runtime.set_caller`
   (el FakeCaller valida cada respuesta contra el output_model — un fake que
   viola el contrato falla el test).
5. **Dos tablas de patrones, un solo prefijo cacheado**: SAS→pandas y
   SAS SQL→T-SQL viven en `llm/prompts/*.md` y entran JUNTAS al system de
   traducción (una entrada de cache para todos los nodos); el user declara
   qué tabla aplica según la strategy derivada del placement.
6. **La auditoría es placement-aware**: sql_pushdown sin full-table-read +
   pandas pesado (high); pandas sin SQL dinámico (high); hybrid con el filtro
   en el WHERE (medium); utility sin I/O de datos (low). Placement efectivo =
   clasificador + overrides de B4b, la misma resolución que usa planning.

## Enforcement

`tests/unit/test_llm_client.py` (retry/refusal/transporte),
`test_needs_human_gate.py` (gates), `test_assembly.py` (estáticos),
`test_llm_nodes.py` (gates reales con FakeCaller + pipeline completo),
`test_audit_placement.py`, y `tests/evals/` (recorded en CI, live con
ANTHROPIC_API_KEY).
