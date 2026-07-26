# Preguntas para Seba

> Decisiones que son tuyas, con recomendación. Nada bloqueó el avance: donde
> hizo falta asumí la recomendación y quedó marcado como revisable.

## Resueltas (2026-07-26)

### Q1 — ¿Reemplazar la vista v1 de inputs/outputs por la v2? → SÍ, reemplazada

Decisión: la vista v2 (parser dirigido) es la **única** fuente de
inputs/outputs. `SASNode.inputs/outputs/libraries` se derivan de
`parse_sas_code` (formato `LIB.TABLA` calificado, WORK explícito);
`lineage.json` matchea por nombre calificado con fallback a nombre corto. El
regex v1 sobrevive solo como chequeo cruzado (`_extract_datasets_legacy`):
`parser_upgrade_report.json` registra por nodo lo recuperado
(`recovered_*`) y los desacuerdos (`lost_*` = lo que el regex veía y el
parser no → flag de revisión). Nota: el residuo de extracción nunca dependió
de esta vista (contabilidad de elementos del .egp), así que no se tocó.

### Q2 — Lista de engines de BD → ampliada y configurable

Decisión: el migrador es genérico. `DEFAULT_DB_ENGINES`
(`core/parser/statements.py`, única definición — se eliminaron las 2 copias)
cubre los engines SAS/ACCESS: ODBC, OLEDB, JDBC, ORACLE, DB2, TERADATA,
SQLSVR, SYBASE, SAPASE, SAPIQ, SAPHANA, INFORMIX, MYSQL, POSTGRES, NETEZZA,
GREENPLM, VERTICA, ASTER, HAWQ, YBRICK, REDSHIFT, SNOW, BIGQUERY, SPARK,
HADOOP, IMPALA, SINGLESTORE, MONGO, SFORCE + alias SQLSRV/MSSQL. Engines
custom del cliente: `parser.extra_db_engines` / `parser.ignore_db_engines`
en `project_config.yaml` (resueltos por `resolve_db_engines(workspace)`).

### Q3 — Placement de nodos 100% macro → categoría `utility`

Decisión: un nodo sin referencias a datos clasifica `utility` (antes
`pandas` con razón "utilitario"). Los nodos que referencian datos **vía
macro** (`&lib..tabla`) siguen siendo `ambiguous` → entrevista B4b (sí tocan
datos, no se sabe dónde viven). No confundir con `classification: utility`
de analyze.py: ese eje dice QUÉ es el nodo; placement dice DÓNDE corre.

### Q4 — Próxima etapa → Etapa 3 confirmada

Se sigue el orden del plan: Etapa 3 = entrevistas como `interrupt()` +
servidor MCP + CLI como cliente de referencia.

## Abiertas

(ninguna)
