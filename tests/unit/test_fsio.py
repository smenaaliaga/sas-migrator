"""fsio — la única implementación de escritura de artefactos, y que siga única.

Tres contratos: (1) el formato de cada variante es byte-idéntico al que los
escritores tenían antes de unificarse (el golden de determinismo compara
artefactos); (2) un kill a mitad de escritura nunca deja un archivo truncado
ni temporales huérfanos; (3) no reaparecen copias de `_dump_json` ni
`write_text(json.dumps(...))` en src/ — la deuda que este módulo saldó.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sas_migrator.core.utils import fsio

SRC_DIR = Path(__file__).resolve().parents[2] / "src" / "sas_migrator"


# ── Formatos byte-idénticos a los escritores históricos ─────────────────────


def test_dump_json_default_matches_historic_indent2(tmp_path: Path) -> None:
    data = {"clave": "ñandú", "lista": [1, 2]}
    out = tmp_path / "a.json"
    fsio.dump_json(out, data)
    assert out.read_bytes() == json.dumps(
        data, indent=2, ensure_ascii=False
    ).encode("utf-8")


def test_dump_json_compact_matches_nodes_index_format(tmp_path: Path) -> None:
    data = {"nodes": [{"id": "x"}]}
    out = tmp_path / "nodes_index.json"
    fsio.dump_json(out, data, indent=None, separators=(",", ":"))
    assert out.read_bytes() == json.dumps(
        data, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def test_dump_json_trailing_newline_matches_schemas_format(tmp_path: Path) -> None:
    out = tmp_path / "x.schema.json"
    fsio.dump_json(out, {"a": 1}, trailing_newline=True)
    assert out.read_text(encoding="utf-8").endswith("}\n")


def test_dump_yaml_unicode_and_insertion_order(tmp_path: Path) -> None:
    out = tmp_path / "q.yaml"
    fsio.dump_yaml(out, {"zeta": "ñ", "alfa": 1})
    text = out.read_text(encoding="utf-8")
    assert "ñ" in text  # allow_unicode
    assert text.index("zeta") < text.index("alfa")  # sort_keys=False


def test_load_json_none_si_no_existe_y_required_lanza(tmp_path: Path) -> None:
    assert fsio.load_json(tmp_path / "no.json") is None
    with pytest.raises(FileNotFoundError):
        fsio.load_json(tmp_path / "no.json", required=True)


# ── Atomicidad: el kill no deja truncados ni huérfanos ───────────────────────


def test_kill_durante_replace_conserva_el_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si el proceso muere entre el temp y el replace, el artefacto viejo
    queda intacto y no sobreviven .tmp — el estado nunca queda a medias."""
    out = tmp_path / "artefacto.json"
    fsio.dump_json(out, {"version": 1})
    original = out.read_bytes()

    def boom(src, dst):
        raise OSError("simulacro de kill")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        fsio.dump_json(out, {"version": 2})
    monkeypatch.undo()

    assert out.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp")), "temporal huérfano tras el fallo"


# ── La deuda no vuelve: una sola implementación en todo src/ ─────────────────


def test_no_reaparecen_escritores_json_fuera_de_fsio() -> None:
    """`write_text(json.dumps(...))` no es atómico: un kill deja un JSON
    truncado que crashea (o miente) aguas abajo. La única implementación
    permitida vive en fsio."""
    offenders: list[str] = []
    for py in SRC_DIR.rglob("*.py"):
        if py.name == "fsio.py":
            continue
        text = py.read_text(encoding="utf-8")
        if "write_text(json.dumps(" in text.replace("\n", "").replace(" ", ""):
            offenders.append(py.name)
        if "def _dump_json" in text or "def _dump_yaml" in text:
            offenders.append(f"{py.name} (def local)")
    assert not offenders, (
        f"escritores JSON duplicados fuera de fsio: {offenders} — usar "
        "sas_migrator.core.utils.fsio"
    )
