# ADR-0009 — La fase 7 son tres sub-nodos: el interrupt vive solo, la ejecución es reanudable

## Contexto

`phase7_validation` era un nodo de 85 líneas con seis responsabilidades:
verificación de tablas contra la BD, staging de referencias, la pausa sagrada
(`interrupt()`), ejecución de notebooks, cascada de validación y diagnóstico.
Por ADR-0002 un nodo con `interrupt()` se re-ejecuta COMPLETO al reanudar.
Consecuencias medidas:

- Cada `resume` de la fase 7 volvía a golpear la BD (verify) aunque nada
  hubiera cambiado.
- Si el proceso moría DURANTE `execute_notebooks`, al reanudar el `__resume__`
  checkpointeado re-alimentaba la autorización y **los notebooks se
  re-ejecutaban** — con escrituras a medias ya aplicadas en la BD. El punto
  más peligroso del sistema estaba exactamente donde el replay es más caro.

## Decisión

1. **Tres sub-nodos encadenados sin gate intermedio** (`PHASES` admite filas
   con fase `None`): `phase7_verify` → `phase7_authorize` →
   `phase7_execute_validate` → `gate7`. El gate sigue siendo el único camino a
   la fase 8 — la garantía "gates como topología" no cambia.
2. **`phase7_authorize` contiene SOLO el interrupt** y ninguna escritura a
   `state/` (ADR-0002 en su forma más pura: no hay efectos previos que
   replicar). La decisión viaja por el estado del grafo
   (`execution_authorized: bool`) — exactamente lo que el checkpointer sabe
   persistir y rebobinar.
3. **La ejecución es idempotente por contenido**: `execute_notebooks` con
   `progress_path` persiste (atómico) el sha256 de las celdas de cada notebook
   con su resultado, DESPUÉS de cada notebook. En el replay, un notebook ya
   PASS con el mismo sha se saltea (`SKIPPED_CACHED`); si el sha cambió
   (re-traducción, edición manual), se re-ejecuta.
4. Los sub-nodos se comunican por disco, como todo el sistema: `verify`
   escribe `table_verification.json`, `execute_validate` lo lee. El staging es
   byte-idempotente y se re-deriva.

## Consecuencias

- Reanudar la respuesta de la tarjeta de autorización re-ejecuta SOLO
  `phase7_authorize` (leer notebooks + re-presentar la tarjeta): la BD no se
  toca y los notebooks no corren dos veces.
- `rewind --phase 7` arranca en `phase7_verify` (`PHASE_ENTRY_NODE`); el
  `as_node` del retry de gate es `phase7_execute_validate` (la fila que
  desemboca en el gate).
- **Migración**: un checkpoint viejo pausado DENTRO de la fase 7
  (`next=("phase7_validation",)`) no reanuda sobre el grafo nuevo — tras
  actualizar, `rewind --phase 7`. Una corrida bloqueada antes de la fase 7 no
  se ve afectada.
