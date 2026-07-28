# ADR-0010 — Economía de la corrida: presupuesto, paralelismo y descartes

Fecha: 2026-07-28 · Estado: aceptada

## Contexto

La corrida real de `Síntesis_M_CR18` (84 nodos) midió tres problemas de
economía: cero paralelismo (75 llamadas secuenciales ≈ 17 minutos solo de
traducción), prompt caching rindiendo exactamente 0 (`cache_read=0` en 95/95
llamadas — prefijo bajo el mínimo cacheable del modelo), y ningún acumulado ni
tope de gasto — "cuánto llevamos" no tenía respuesta y un loop desbocado
gastaba hasta que alguien mirara.

## Decisión

1. **Presupuesto primero, paralelismo después.** `llm.max_run_cost_usd` corta
   ANTES de cada llamada con `BudgetExceeded` (reanudable — nunca NeedsHuman:
   el checkpointer y las traducciones persistidas quedan intactos). El
   acumulado se deriva SIEMPRE del trace en disco (`llm_trace.jsonl`,
   append-only): sobrevive reinicios sin estado extra. El tope existe antes de
   multiplicar el gasto por N workers.
2. **Paralelismo opt-in por ThreadPool** (`llm.max_workers`, default 1). El
   primer nodo va secuencial — escribe el prompt cache que el resto lee — y el
   ensamblado final es siempre secuencial en el orden del plan: los notebooks
   son byte-idénticos con cualquier N (hay test). La persistencia real es por
   archivo de traducción, así que un Ctrl-C conserva lo hecho.
3. **Cache escalonado + few-shot** para que el prefijo supere el mínimo
   cacheable y un cambio en el contexto del proyecto no invalide el prefijo
   compartido (un breakpoint por bloque system, ≤4 del API).
4. **Modelo y tope de salida por tarea** (`llm.models_by_task`,
   `llm.max_tokens_by_task`): la traducción amerita el modelo fuerte; matching
   y docs no necesitan pagarlo.

## Descartado con motivo

- **Batch API de Anthropic** (50% de descuento): no existe en Foundry — uno de
  los dos backends soportados — y rompe el flujo interactivo: la fase 6
  alimenta el retry con memoria y el verificador con la respuesta ANTERIOR de
  cada nodo, y una tanda batch de horas no puede reaccionar por nodo. Si algún
  día hay una fase realmente offline (p. ej. re-traducción masiva nocturna),
  reevaluar.
- **Send API de LangGraph** (fan-out por super-steps): no aporta sobre el
  ThreadPool. La unidad de reanudación de la fase 6 no es el checkpointer sino
  el archivo de traducción por nodo (`state/translations/*.json`); meter el
  fan-out en el grafo agregaría serialización de estado por nodo traducido y
  un checkpoint gordo, sin ganar nada en recuperación — que ya es por archivo.

## Consecuencias

- `status` responde costo (~$), tokens in/out, % de cache y confianza sin
  abrir un solo artefacto.
- Un modelo sin precio en la tabla cuenta como `unpriced` y se DECLARA en el
  resumen — nunca se inventa una tarifa.
- `max_workers > 1` cambia el orden de las llamadas (no de los notebooks);
  el trace deja de ser cronológicamente 1:1 con el orden del plan.
