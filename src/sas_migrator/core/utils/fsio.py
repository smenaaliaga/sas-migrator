"""Lectura tolerante y escritura ATÓMICA de artefactos — única implementación.

Antes había seis copias de `_dump_json` y solo la de las entrevistas era
atómica. Un kill a mitad de un `write_text` deja un JSON truncado, y un
artefacto truncado no falla el gate: lo crashea (o peor, miente aguas abajo).
Acá vive la versión buena — temp en el MISMO directorio + `os.replace` — y un
test vigila que no vuelvan a aparecer copias.

Los kwargs de formato de `dump_json` existen porque tres artefactos tienen
formato propio con golden encima (nodes_index compacto, db_evidence indent=1,
schemas con newline final): unificar el escritor no puede cambiar ni un byte
del contenido.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml


def load_json(path: Path, *, required: bool = False) -> Any:
    """JSON de disco; None si no existe (o FileNotFoundError con required)."""
    path = Path(path)
    if not path.exists():
        if required:
            raise FileNotFoundError(path)
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_yaml(path: Path, *, required: bool = False) -> Any:
    """YAML de disco; None si no existe (o FileNotFoundError con required)."""
    path = Path(path)
    if not path.exists():
        if required:
            raise FileNotFoundError(path)
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def atomic_write_text(path: Path, text: str) -> None:
    """Escritura temp+os.replace: el artefacto nunca queda a medio escribir.

    El temp va en el MISMO directorio que el destino — `os.replace` entre
    volúmenes distintos no es atómico. Siempre LF: los artefactos se comparan
    entre corridas y entre plataformas.
    """
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def dump_json(
    path: Path,
    data: Any,
    *,
    indent: int | None = 2,
    separators: tuple[str, str] | None = None,
    trailing_newline: bool = False,
    default: Any = None,
) -> None:
    """JSON atómico; por defecto `indent=2, ensure_ascii=False` (el formato
    del 90% de los artefactos de state/)."""
    text = json.dumps(
        data, indent=indent, ensure_ascii=False, separators=separators,
        default=default,
    )
    if trailing_newline:
        text += "\n"
    atomic_write_text(path, text)


def dump_yaml(path: Path, data: Any) -> None:
    """YAML atómico (safe_dump, unicode, orden de inserción)."""
    atomic_write_text(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
