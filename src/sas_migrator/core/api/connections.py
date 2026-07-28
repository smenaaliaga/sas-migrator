"""Loader único de ``state/api_connections.yaml``.

Espejo de ``core/db/connections.py`` para las conexiones externas (APIs HTTP)
que decide la entrevista B4c: lista de conexiones, ``[]`` si el archivo no
existe o no tiene ninguna.
"""

from __future__ import annotations

from pathlib import Path

from sas_migrator.core.utils.fsio import load_yaml

FILENAME = "api_connections.yaml"


def load_api_connections(state_dir: Path | str) -> list[dict]:
    """Conexiones externas declaradas por la entrevista B4c; ``[]`` si no hay."""
    doc = load_yaml(Path(state_dir) / FILENAME) or {}
    conns = doc.get("connections", []) if isinstance(doc, dict) else []
    return [c for c in conns if isinstance(c, dict)]


def sdk_packages(state_dir: Path | str) -> list[str]:
    """Nombres de import de los SDK elegidos (mode=sdk), sin duplicados, ordenados."""
    packages = {
        str(c["package"]).strip()
        for c in load_api_connections(state_dir)
        if c.get("mode") == "sdk" and str(c.get("package", "")).strip()
    }
    return sorted(packages)


def sdk_by_host(state_dir: Path | str) -> dict[str, str]:
    """host → package para las conexiones con mode=sdk."""
    return {
        str(c["host"]).strip().lower(): str(c["package"]).strip()
        for c in load_api_connections(state_dir)
        if c.get("mode") == "sdk" and c.get("host") and str(c.get("package", "")).strip()
    }
