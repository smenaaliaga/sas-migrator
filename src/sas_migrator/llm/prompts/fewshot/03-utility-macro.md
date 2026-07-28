# Ejemplo 3 — strategy `python` (utility): macros sin datos

## Entrada (user)

```json
{"node_id": "macro-utility", "node_label": "Parámetros de periodo", "strategy": "python", "placement": "utility", "macro_params": ["periodo"]}
```

```sas
%let periodo = 202601;
%macro ultimo_dia(anio, mes);
  %sysfunc(day(%sysfunc(intnx(month, %sysfunc(mdy(&mes,1,&anio)), 0, e))))
%mend ultimo_dia;
```

## Salida (NodeTranslation)

```json
{
  "node_id": "macro-utility",
  "node_label": "Parámetros de periodo",
  "strategy": "python",
  "imports": ["import calendar"],
  "cells": [
    "def ultimo_dia(anio: int, mes: int) -> int:\n    return calendar.monthrange(anio, mes)[1]\n"
  ],
  "traceability": {
    "sas_construct": "%LET + %MACRO utilitaria de fechas",
    "business_rule": "Declara el periodo de proceso y el último día de un mes"
  },
  "confidence": "high",
  "warnings": []
}
```

Nota: `utility` es Python plano sin I/O de datos. El `%let periodo` no se fija
como literal: `periodo` ya está en `macro_params` y lo define la celda de
parámetros del notebook.
