"""Infra de snapshots golden — protege el UX lean por regresión.

Los snapshots viven en ``tests/golden/snapshots/*.json`` (pretty, revisables
en diff). Para regenerarlos tras un cambio INTENCIONAL de payload:

    UPDATE_SNAPSHOTS=1 python -m pytest tests/golden -q
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sas_migrator.graph.builder import build_graph, initial_state
from sas_migrator.testing.egp_builder import build_egp

SNAP_DIR = Path(__file__).parent / "snapshots"
REGEN_HINT = "regenerar con: UPDATE_SNAPSHOTS=1 python -m pytest tests/golden -q"


@pytest.fixture(scope="session")
def synthetic_state(tmp_path_factory) -> Path:
    """Workspace del .egp sintético con el pipeline stub completo ya corrido."""
    ws = tmp_path_factory.mktemp("golden_ws")
    (ws / "input" / "egp").mkdir(parents=True)
    (ws / "input" / "data").mkdir()
    (ws / "input" / "docs").mkdir()
    egp = build_egp(ws / "input" / "egp" / "demo.egp")
    result = build_graph().invoke(initial_state(ws, egp))
    assert result["done"] is True, "el pipeline stub debe completar para poder snapshotear"
    return ws / "state"


@pytest.fixture
def snapshot():
    """Compara `data` byte a byte contra snapshots/<name>.json."""

    def check(name: str, data) -> None:
        path = SNAP_DIR / f"{name}.json"
        rendered = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        if os.environ.get("UPDATE_SNAPSHOTS") == "1":
            SNAP_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8", newline="\n")
            return
        assert path.exists(), f"snapshot faltante: {path.name} — {REGEN_HINT}"
        current = path.read_text(encoding="utf-8")
        assert current == rendered, (
            f"payload de '{name}' cambió respecto del snapshot — si el cambio es "
            f"intencional, {REGEN_HINT}"
        )

    return check
