"""Loader único de ``state/db_connections.yaml``.

Siete módulos leían el YAML por su cuenta (cascade, execution, profile,
verify_tables, interview/execution, llm/phases ×2), cada uno con su manejo de
"no existe" y su forma del documento. Un solo lector con una sola semántica:
lista de conexiones, ``[]`` si el archivo no existe o no tiene ninguna.
"""

from __future__ import annotations

from pathlib import Path

from sas_migrator.core.utils.fsio import load_yaml

FILENAME = "db_connections.yaml"


def load_connections(state_dir: Path | str) -> list[dict]:
    """Conexiones declaradas por la entrevista B4b; ``[]`` si no hay."""
    doc = load_yaml(Path(state_dir) / FILENAME) or {}
    conns = doc.get("connections", []) if isinstance(doc, dict) else []
    return [c for c in conns if isinstance(c, dict)]


def connection_aliases(state_dir: Path | str) -> list[str]:
    """Solo los alias (= librefs SAS), en el orden del archivo."""
    return [str(c["alias"]) for c in load_connections(state_dir) if c.get("alias")]
