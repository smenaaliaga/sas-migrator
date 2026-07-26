# ADR-0004 — El UX lean se protege por snapshot, no por disciplina

**Estado**: aceptada (Etapa 3)

## Decisión

Cada payload de entrevista tiene un snapshot golden byte a byte en
`tests/golden/snapshots/` (regeneración explícita con `UPDATE_SNAPSHOTS=1`).
Las invariantes del estilo lean son asserts, no guías:

- sin recaps: `transition` es UNA línea corta (≤90 chars);
- toda pregunta con opciones tiene `recommended_default` válido — el camino
  fácil ("no sé" / Enter) nunca exige escribir;
- una ficha M-xxx por tarjeta, con exactamente `Aprobar / Rechazar /
  Explicar más` (postponed jamás aparece como opción);
- sin evidencia no hay tarjeta (mapping/preprocesamiento/ambigüedades/B4b se
  omiten si el análisis no encontró nada);
- B4b pregunta por **causa raíz** (prefijo sin confirmar), no por nodo; un
  prefijo confirmado por LIBNAME no genera tarjeta.

## Por qué

El estilo conversacional se degrada por acumulación de "mejoras" pequeñas
(un recap aquí, una pregunta extra allá). Un snapshot que falla convierte esa
degradación en una decisión consciente y revisable en el diff.

## Enforcement

`tests/golden/test_payload_snapshots.py` (dos guiones: default y con ramas) +
las invariantes `test_every_choice_question_has_valid_recommended_default` y
`test_transitions_are_micro_lines_not_recaps`.
