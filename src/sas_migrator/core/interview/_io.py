"""Lectura tolerante y escritura atómica de artefactos de state/."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml


def load_json(path: Path) -> Any:
    """JSON de disco; None si no existe."""
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_yaml(path: Path) -> Any:
    """YAML de disco; None si no existe."""
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _atomic_write(path: Path, text: str) -> None:
    """Escritura temp+rename: el artefacto nunca queda a medio escribir."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_yaml(path: Path, data: Any) -> None:
    _atomic_write(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))


def atomic_write_json(path: Path, data: Any) -> None:
    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False))
