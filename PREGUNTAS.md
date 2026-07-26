# Preguntas para Seba

> Decisiones que son tuyas, con recomendación. Nada bloqueó el avance: donde
> hizo falta asumí la recomendación y quedó marcado como revisable.

## Q1 — ¿Reemplazar la vista v1 de inputs/outputs por la v2?

Hoy conviven: `flow_graph`/`lineage` usan el extractor v1 (regex, incompleto
pero es la base del residuo de extracción testeado), y `nodes_index` tiene la
vista v2 (`inputs_v2/outputs_v2`, mucho más completa). **Recomendación:**
migrar `lineage.json` a la vista v2 en la Etapa 4 (mejor cobertura de linaje)
manteniendo el residuo sobre v1, y comparar ambas vistas como chequeo cruzado
(desacuerdo = flag de revisión). Alternativa conservadora: dejar ambas vistas
y decidir con tu .egp real.

## Q2 — Lista de engines de BD

`DB_ENGINES = {ODBC, OLEDB, SQLSVR, ORACLE, TERADATA, POSTGRES}` (heredada de
v1, ahora en un solo lugar: `core/parser/statements.py`). ¿Falta alguno que
uses (p. ej. `SQLSRV`, `ODBCLOAD`, engines con alias custom)?

## Q3 — Placement de nodos 100% macro

Un nodo cuyo código es solo `%let` / definiciones `%macro` clasifica como
`pandas` con razón "utilitario". Si prefieres una categoría propia
(`utility`), es un cambio chico en `placement.py`.

## Q4 — Próxima etapa

El plan dice Etapa 3 = entrevistas como `interrupt()` + servidor MCP + CLI
como cliente. ¿Confirmas ese orden, o prefieres priorizar la Etapa 4
(traducción LLM + ensamblador) para ver antes valor con tu .egp real?
