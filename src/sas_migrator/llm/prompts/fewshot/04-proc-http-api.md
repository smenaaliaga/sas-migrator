# Ejemplo 4 — strategy `pandas`: PROC HTTP a una API + LIBNAME json

## Entrada (user)

```json
{"node_id": "proc-http-api", "node_label": "Serie UF desde API BDE", "strategy": "pandas", "placement": "pandas", "input_datasets": [], "output_datasets": ["WORK.UF_DIARIA"]}
```

```sas
filename resp temp;
proc http
  url="https://si3.bcentral.cl/SieteRestWS/SieteRestWS.ashx?user=&user.%nrstr(&pass)=&password.%nrstr(&function)=GetSeries%nrstr(&timeseries)=F073.UFF.PRE.Z.D%nrstr(&firstdate)=2026-01-01"
  method="get" out=resp;
run;

libname api json fileref=resp automap=create;

data work.uf_diaria;
  set api.series_obs;
  fecha = input(substr(indexdatestring, 1, 10), ddmmyy10.);
  valor = input(value, best.);
  keep fecha valor;
run;
```

## Salida (NodeTranslation)

```json
{
  "node_id": "proc-http-api",
  "node_label": "Serie UF desde API BDE",
  "strategy": "pandas",
  "imports": ["import os", "import requests"],
  "cells": [
    "r = requests.get(\n    \"https://si3.bcentral.cl/SieteRestWS/SieteRestWS.ashx\",\n    params={\n        \"user\": os.environ[\"BDE_USER\"],\n        \"pass\": os.environ[\"BDE_PASS\"],\n        \"function\": \"GetSeries\",\n        \"timeseries\": \"F073.UFF.PRE.Z.D\",\n        \"firstdate\": \"2026-01-01\",\n    },\n    timeout=30,\n)\nr.raise_for_status()\n",
    "uf_diaria = pd.json_normalize(r.json()[\"Series\"][\"Obs\"])\nuf_diaria[\"fecha\"] = pd.to_datetime(uf_diaria[\"indexDateString\"].str[:10], format=\"%d-%m-%Y\")\nuf_diaria[\"valor\"] = pd.to_numeric(uf_diaria[\"value\"], errors=\"coerce\")\nuf_diaria = uf_diaria[[\"fecha\", \"valor\"]]\n"
  ],
  "traceability": {
    "sas_construct": "PROC HTTP GET + LIBNAME json automap + DATA step de parseo",
    "business_rule": "Descarga la serie diaria de UF desde la API BDE y normaliza fecha/valor"
  },
  "confidence": "high",
  "warnings": [
    "credenciales &user/&password → os.environ (BDE_USER/BDE_PASS); el literal rompe el scanner de secretos",
    "indexDateString viene dd-MM-yyyy y value como string: parseo explícito, no inferido"
  ]
}
```

Nota: la URL literal del SAS se descompone en `params` (mismo host, mismo
método) — nunca se arma por f-string. Si el contexto del proyecto declara ese
host con `mode=sdk`, se usa SOLO el paquete indicado (ej. `bcchapi`) en lugar
de `requests`.
