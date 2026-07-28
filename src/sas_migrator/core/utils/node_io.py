"""Codec de nodes/<id>.json — el código SAS se guarda como array de líneas.

En disco, el campo ``code`` (un string con ``\\n`` escapados, ilegible en el
JSON) se serializa como ``code_lines``: una línea por elemento, al estilo del
``source`` de los notebooks Jupyter. En memoria el contrato no cambia: todo
consumidor sigue viendo ``code: str``. Este módulo es el ÚNICO punto donde
viven ambas representaciones — leer un nodo sin pasar por acá arriesga ver
``code_lines`` crudo, escribirlo sin pasar por acá vuelve al string ilegible.

El split es por ``"\\n"`` literal (no ``splitlines()``): ``splitlines`` también
corta en ``\\r``, ``\\x85`` y separadores unicode, y el round-trip dejaría de
ser byte a byte. Un ``\\r`` embebido queda visible escapado en su línea — feo
pero fiel. La lectura tolera el formato viejo (``code`` string) para no exigir
re-extracción de workspaces existentes.
"""

from __future__ import annotations

from pathlib import Path

from sas_migrator.core.utils.fsio import dump_json, load_json

CODE_KEY = "code"
CODE_LINES_KEY = "code_lines"


def encode_node_code(node: dict) -> dict:
    """Copia del nodo con ``code`` → ``code_lines`` (formato de disco)."""
    out = dict(node)
    code = out.pop(CODE_KEY, None)
    if code is not None:
        out[CODE_LINES_KEY] = str(code).split("\n") if code else []
    return out


def decode_node_code(node: dict) -> dict:
    """Copia del nodo con ``code_lines`` → ``code`` (contrato en memoria).

    Tolera el formato viejo: si ya trae ``code`` string, se respeta.
    """
    out = dict(node)
    lines = out.pop(CODE_LINES_KEY, None)
    if CODE_KEY not in out:
        out[CODE_KEY] = "\n".join(lines) if lines else ""
    return out


def load_node(path: Path, *, required: bool = False) -> dict | None:
    """nodes/<id>.json de disco, ya decodificado; None si no existe."""
    doc = load_json(path, required=required)
    if not isinstance(doc, dict):
        return doc
    return decode_node_code(doc)


def dump_node(path: Path, node: dict) -> None:
    """Escritura atómica del nodo, codificado a ``code_lines``."""
    dump_json(path, encode_node_code(node))
