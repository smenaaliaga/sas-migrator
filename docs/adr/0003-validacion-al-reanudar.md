# ADR-0003 — Las respuestas se validan al reanudar; los YAML los escribe código

**Estado**: aceptada (Etapa 3)

## Decisión

1. Al reanudar, la respuesta cruda pasa por dos capas: forma
   (`CardAnswers.model_validate`, Pydantic) y contenido
   (`core.interview.validate.validate_answers`: ids conocidos, opción ∈
   options, requeridas cubiertas, texto libre solo si la tarjeta lo admite).
2. Una respuesta inválida **nunca es un crash**: se re-presenta la misma
   tarjeta con `validation_error` poblado (nuevo `interrupt()`).
3. Los artefactos de decisión (`initial_interview.yaml`,
   `post_analysis_interview.yaml`, `ignored_nodes.yaml`,
   `approved_improvements.yaml`, `db_connections.yaml`,
   `placement_decisions.yaml`) los escribe **siempre código determinista**
   (`core.interview.apply`, temp+rename, modelos Pydantic = válidos contra
   schema por construcción). Nunca un LLM ni texto del usuario sin procesar.
4. `postponed` no es una opción de tarjeta: solo se registra si el usuario lo
   pidió en texto libre (regla léxica en `apply.decide_improvement`). Una
   contrapropuesta se re-presenta como tarjeta modificada; jamás se aplica en
   silencio.

## Por qué

El usuario puede responder desde clientes arbitrarios (CLI, VS Code, Claude
Code vía MCP): el único punto confiable de validación es el resume. Y los
gates validan schema — si un humano redactara los YAML, el contrato schema-
modelo dejaría de estar garantizado por construcción.

## Enforcement

`tests/unit/test_interview_apply.py` (gates 1/4/5 reales + negativos) y
`tests/unit/test_interrupts.py::test_invalid_answer_reinterrupts_with_validation_error`.
