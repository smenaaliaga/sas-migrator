# ADR-0008 — Observabilidad LLM local y harness del .egp real

## Estado

Aceptada (Etapa 6).

## Contexto

Con las fases LLM en producción (2/3/6/7/8) hacen falta dos cosas que el
código no daba: saber **cómo se está comportando el LLM** a lo largo del
tiempo (¿más retries? ¿más needs_human tras cambiar un prompt?) y validar el
sistema contra un **.egp productivo real**, no solo el sintético. El plan
canónico menciona Langfuse como opción de observabilidad y exige el golden
real como DoD de la etapa.

## Decisión

1. **Trazas locales, no servicio externo.** `TracingCaller` (decorador del
   `StructuredCaller` real) escribe una línea JSON por llamada en
   `state/llm_trace.jsonl`: task, `prompt_hash` (sha256 truncado del system —
   identifica la versión del prompt sin copiarlo), modelo, outcome
   (`ok` / `needs_human:<reason>` / `error:<Type>`), intentos, duración y
   usage. Se envuelve **solo** el `AnthropicCaller` real en
   `runtime.get_caller`; los fakes inyectados con `set_caller` quedan sin
   envolver (los tests de identidad y el golden no cambian). Langfuse queda
   como integración futura si algún día hace falta UI — el formato jsonl es
   trivial de exportar.
2. **Harness del .egp real como tests de integración env-gated**
   (`SASMIG_REAL_EGP`), en tres niveles independientes: extracción+parser
   (invariantes duros: nunca crashea, residuo cuadrado), pipeline estructural
   completo en stub_mode, y traducción live nodo a nodo. Los desvíos **no
   rompen el harness**: se acumulan en un backlog JSON
   (`SASMIG_REAL_EGP_REPORT`) que se convierte en casos de eval y fixes —
   regla anti-drift: un desvío del LLM produce una verificación nueva, nunca
   "más prompt".
3. **Tres velocidades de suite**: rápida (CI por push), evals live y harness
   real (nightly / manual con `workflow_dispatch`, backlog como artifact).

## Consecuencias

- El diagnóstico "el traductor anda peor esta semana" es un diff de
  `llm_trace.jsonl`, no una anécdota.
- El `.egp` real nunca entra al repo: el harness lo recibe por variable de
  entorno y el nightly por input del dispatch.
- El eval set crece dirigido por el backlog del harness (bugs reales), con
  target 30–50 casos.
