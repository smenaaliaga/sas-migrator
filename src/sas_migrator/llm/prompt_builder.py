"""Construcción de prompts desde los recursos del paquete.

El system de traducción es UN prefijo estable (convenciones + AMBAS tablas de
patrones): una sola entrada de prompt-cache para todos los nodos, sin importar
la strategy — el mensaje user declara qué tabla aplica. Todo lo volátil viaja
en el user.
"""

from __future__ import annotations

import json
from importlib.resources import files


def _read(name: str) -> str:
    return (files("sas_migrator.llm") / "prompts" / name).read_text(encoding="utf-8")


def build_analysis_system() -> list[str]:
    return [_read("analysis_pfd.md")]


def build_improvements_system() -> list[str]:
    return [_read("analysis_improvements.md")]


def build_matching_system() -> list[str]:
    return [_read("matching.md")]


def build_diagnosis_system() -> list[str]:
    return [_read("mismatch_diagnosis.md")]


def build_docs_system() -> list[str]:
    return [_read("doc_writer.md")]


def build_translation_system() -> list[str]:
    return [
        _read("translation_system.md")
        + "\n\n"
        + _read("patterns_sas_pandas.md")
        + "\n\n"
        + _read("patterns_sas_tsql.md")
    ]


def header_line(payload: dict) -> str:
    """Primera línea del mensaje user: contexto estructurado en JSON.

    Da al modelo (y a los fakes de test) los identificadores exactos que el
    output debe ecoar (node_id, strategy, pfd_id...).
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def build_translation_user(
    target: dict, node_code: str, db_aliases: list[str]
) -> str:
    head = header_line(
        {
            "node_id": target.get("node_id"),
            "node_label": target.get("node_label", ""),
            "strategy": target.get("strategy", "pandas"),
            "placement": target.get("placement"),
            "dependencies": target.get("dependencies", []),
            "approved_improvements": target.get("approved_improvements", []),
            "db_aliases": db_aliases,
        }
    )
    return (
        head
        + "\n\nCódigo SAS del nodo:\n```sas\n"
        + (node_code or "(sin código)")
        + "\n```\n"
    )
