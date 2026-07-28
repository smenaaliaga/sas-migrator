"""Lectura tolerante y escritura atómica de artefactos de state/.

La implementación vive en ``core.utils.fsio`` (única copia para todo el repo);
este módulo re-exporta con los nombres históricos para no tocar los imports
de las entrevistas.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sas_migrator.core.utils.fsio import (  # noqa: F401  (re-export)
    dump_json,
    dump_yaml,
    load_json,
    load_yaml,
)


def atomic_write_yaml(path: Path, data: Any) -> None:
    dump_yaml(path, data)


def atomic_write_json(path: Path, data: Any) -> None:
    dump_json(path, data)
