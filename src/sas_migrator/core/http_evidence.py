"""Extracción de evidencia HTTP desde el código SAS de los nodos.

El endpoint que consulta un nodo está escrito en su propio código (`URL=` de
PROC HTTP o `FILENAME f URL "..."`). Este módulo es la única implementación de
esa lectura: la usan el índice de fase 2 (`analysis/indexes.py`, que persiste
`state/http_evidence.json` para la entrevista B4c) y la auditoría de fase 6
(`audit.py`, reglas de deriva de origen).

Una URL armada con macro variables (``&base./ws``) no aporta host literal y se
declara en ``nodes_with_dynamic_url``: mejor no inferir que inferir mal.
"""

from __future__ import annotations

import re
from typing import Any

# Cubre `PROC HTTP URL="..."` y `FILENAME f URL "..."` (este sin `=`).
_URL_RE = re.compile(r"""\burl\s*=?\s*(["'])(.+?)\1""", flags=re.IGNORECASE)
_SCHEME_HOST_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://([^/\s?\"']+)", flags=re.IGNORECASE)
_METHOD_RE = re.compile(r"""\bmethod\s*=\s*(["']?)(\w+)\1""", flags=re.IGNORECASE)
_PROC_HTTP_RE = re.compile(r"\bPROC\s+HTTP\b", flags=re.IGNORECASE)
_FILENAME_URL_RE = re.compile(r"\bFILENAME\s+\w+\s+URL\b", flags=re.IGNORECASE)

_MAX_SAMPLE_URLS = 3


def _host_of(raw_url: str) -> str:
    """Host literal de una URL, o '' si no se puede leer sin adivinar."""
    match = _SCHEME_HOST_RE.match(raw_url.strip())
    if not match:
        return ""
    host = match.group(1).split("@")[-1].split(":")[0].strip().lower()
    if not host or "&" in host or "%" in host:
        return ""
    return host


def extract_http_hosts(sas_code: str) -> list[str]:
    """Hosts que el nodo consulta por HTTP, leídos del URL= de su propio código.

    Una URL armada con macro variables (``&base./ws``) no aporta host literal y
    se descarta: mejor no inferir que inferir mal y reportar un falso positivo.
    """
    hosts: list[str] = []
    for _, raw in _URL_RE.findall(sas_code):
        host = _host_of(raw)
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def extract_http_calls(sas_code: str) -> dict[str, Any]:
    """Lectura enriquecida del HTTP de UN nodo: hosts, métodos, URLs de muestra.

    Devuelve::

        {"calls": [{"host", "sample_urls"}...],
         "methods": [...], "sources": [...], "has_dynamic_url": bool}

    ``sources`` describe el mecanismo SAS (``proc_http`` / ``filename_url``) a
    nivel de nodo — el código SAS no ata cada URL= a su statement sin un parser
    completo, y para la entrevista alcanza saber cómo consulta el nodo.
    """
    calls: list[dict[str, Any]] = []
    has_dynamic = False
    for _, raw in _URL_RE.findall(sas_code):
        url = raw.strip()
        host = _host_of(url)
        if not host:
            has_dynamic = True
            continue
        entry = next((c for c in calls if c["host"] == host), None)
        if entry is None:
            entry = {"host": host, "sample_urls": []}
            calls.append(entry)
        if url not in entry["sample_urls"] and len(entry["sample_urls"]) < _MAX_SAMPLE_URLS:
            entry["sample_urls"].append(url)

    sources: list[str] = []
    if _PROC_HTTP_RE.search(sas_code):
        sources.append("proc_http")
    if _FILENAME_URL_RE.search(sas_code):
        sources.append("filename_url")

    methods = sorted({m.group(2).upper() for m in _METHOD_RE.finditer(sas_code)})
    return {
        "calls": calls,
        "methods": methods,
        "sources": sources,
        "has_dynamic_url": has_dynamic,
    }


def build_http_evidence(nodes: list[dict], generated_at: str) -> dict[str, Any]:
    """Agrega la evidencia HTTP de todos los nodos → contenido de http_evidence.json.

    Todo ordenado (hosts, node_ids, methods) para determinismo byte a byte.
    """
    by_host: dict[str, dict[str, Any]] = {}
    dynamic_nodes: list[str] = []
    for n in nodes:
        parsed = extract_http_calls(n.get("code", ""))
        if parsed["has_dynamic_url"] and n["id"] not in dynamic_nodes:
            dynamic_nodes.append(n["id"])
        for call in parsed["calls"]:
            entry = by_host.setdefault(call["host"], {
                "host": call["host"],
                "node_ids": set(),
                "methods": set(),
                "sample_urls": [],
                "sources": set(),
            })
            entry["node_ids"].add(n["id"])
            entry["methods"].update(parsed["methods"])
            entry["sources"].update(parsed["sources"])
            for url in call["sample_urls"]:
                if url not in entry["sample_urls"] and len(entry["sample_urls"]) < _MAX_SAMPLE_URLS:
                    entry["sample_urls"].append(url)

    hosts = [
        {
            "host": h["host"],
            "node_ids": sorted(h["node_ids"]),
            "methods": sorted(h["methods"]),
            "sample_urls": h["sample_urls"],
            "sources": sorted(h["sources"]),
        }
        for _, h in sorted(by_host.items())
    ]
    return {
        "generated_at": generated_at,
        "hosts": hosts,
        "nodes_with_dynamic_url": sorted(dynamic_nodes),
        "detection_notes": [
            "hosts leídos del URL= literal del propio SAS (PROC HTTP / FILENAME URL)",
            "URLs armadas con macro variables quedan declaradas en nodes_with_dynamic_url, nunca inferidas",
        ],
    }
