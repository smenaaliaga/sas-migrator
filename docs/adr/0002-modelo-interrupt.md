# ADR-0002 — Una tarjeta por interrupt; nodos HITL re-ejecutables

**Estado**: aceptada (Etapa 3)

## Decisión

1. La unidad de conversación es la **tarjeta** (`InterviewCard`, 1..n
   preguntas presentadas juntas): un `interrupt()` = una tarjeta. B5 presenta
   una ficha M-xxx por tarjeta; las tarjetas condicionales son llamadas
   `interrupt()` posteriores dentro del mismo nodo.
2. Un nodo con `interrupt()` se **re-ejecuta completo** al reanudar
   (semántica de LangGraph). Por eso:
   - los builders de tarjetas son deterministas y solo leen `state/`;
   - **ninguna escritura de decisión ocurre antes del último interrupt del
     nodo** — las respuestas se acumulan en local y `core.interview.apply`
     escribe todo al final;
   - las escrituras previas a un interrupt se permiten solo si son
     idempotentes entre replays (caso `phase5_plan`: el plan se construye
     una única vez — `if not exists`);
   - la validación de respuestas es determinista (sin reloj ni red): el
     replay revalida lo mismo y converge.
3. El payload se serializa con `model_dump(mode="json")` y **no lleva
   timestamps**: debe ser JSON-serializable para el checkpointer sqlite y
   byte-estable para los snapshots.

## Por qué

Múltiples preguntas por interrupt reducen roundtrips sin perder el "una
decisión a la vez" (que aplica a decisiones, no a campos). El contrato de
re-ejecución evita la clase entera de bugs de "se aplicó la mitad de la
entrevista" tras un reinicio.

## Enforcement

`tests/unit/test_interrupts.py` (pipeline completo por `Command(resume=...)`,
reanudación a mitad de entrevista sobre SqliteSaver reconstruyendo el grafo)
y `tests/golden/` (payloads byte a byte).
