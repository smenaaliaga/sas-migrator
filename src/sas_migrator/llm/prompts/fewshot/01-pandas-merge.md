# Ejemplo 1 — strategy `pandas`: DATA step MERGE

## Entrada (user)

```json
{"node_id": "data-step-merge", "node_label": "Cruza clientes y ventas", "strategy": "pandas", "placement": "pandas", "input_datasets": ["WORK.CLIENTES", "WORK.VENTAS"], "output_datasets": ["WORK.CRUCE"]}
```

```sas
data work.cruce;
  merge work.clientes(in=a) work.ventas(in=b);
  by id_cliente;
  if a and b;
run;
```

## Salida (NodeTranslation)

```json
{
  "node_id": "data-step-merge",
  "node_label": "Cruza clientes y ventas",
  "strategy": "pandas",
  "imports": [],
  "cells": [
    "cruce = clientes.merge(ventas, on=\"id_cliente\", how=\"inner\")\n"
  ],
  "traceability": {
    "sas_construct": "DATA step MERGE con in= (intersección)",
    "business_rule": "Cruza clientes con sus ventas conservando solo coincidencias"
  },
  "confidence": "high",
  "warnings": [
    "MERGE BY de SAS exige orden previo; merge de pandas no lo requiere"
  ]
}
```

Nota: los DataFrames `clientes`/`ventas` NO se recrean — vienen de nodos
anteriores con el nombre del dataset SAS en minúsculas (`input_datasets`).
