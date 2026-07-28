# Ejemplo 2 — strategy `sql_pushdown`: JOIN + escritura idempotente

## Entrada (user)

```json
{"node_id": "proc-sql-join", "node_label": "Resumen por región", "strategy": "sql_pushdown", "placement": "sql_pushdown", "input_datasets": ["GOB.VENTAS", "GOB.SUCURSALES"], "output_tables": ["GOB.RESUMEN"], "macro_params": ["periodo"], "db_aliases": ["GOB"]}
```

```sas
proc sql;
  create table gob.resumen as
  select a.id, b.region, sum(a.monto) as total
  from gob.ventas a
  inner join gob.sucursales b on a.suc = b.suc
  where a.periodo = &periodo
  group by a.id, b.region;
quit;
```

## Salida (NodeTranslation)

```json
{
  "node_id": "proc-sql-join",
  "node_label": "Resumen por región",
  "strategy": "sql_pushdown",
  "imports": [],
  "cells": [
    "sql_resumen = \"\"\"\nSELECT a.id, b.region, SUM(a.monto) AS total\nFROM GOB.dbo.VENTAS a\nINNER JOIN GOB.dbo.SUCURSALES b ON a.suc = b.suc\nWHERE a.periodo = ?\nGROUP BY a.id, b.region\n\"\"\"\nresumen = pd.read_sql(sql_resumen, engine, params=(periodo,))\n",
    "with engine.begin() as conn:\n    conn.exec_driver_sql(\"DELETE FROM GOB.dbo.RESUMEN WHERE periodo = ?\", (periodo,))\nresumen.to_sql(\"RESUMEN\", engine, schema=\"dbo\", if_exists=\"append\", index=False)\n"
  ],
  "traceability": {
    "sas_construct": "PROC SQL CREATE TABLE con INNER JOIN y agregación",
    "business_rule": "Resumen de montos por cliente y región del periodo, escrito idempotente"
  },
  "confidence": "medium",
  "warnings": [
    "El nombre de base GOB asume el mapeo por defecto libref→base (db_connections.yaml)"
  ]
}
```

Nota: el reemplazo estilo SAS es `DELETE FROM` + append — nunca `DROP TABLE`
ni `if_exists="replace"`. `&periodo` es `macro_params`: se usa el NOMBRE
(`periodo`, definido en la celda de parámetros), jamás el literal, y viaja
como parámetro del SQL (`?`), nunca interpolado en un f-string.
