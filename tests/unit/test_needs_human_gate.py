"""needs_human: nunca silencio — un item sin resolver bloquea el gate de su fase."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from sas_migrator.core.utils import needs_human
from sas_migrator.core.utils.schema_validation import check_gate


def _minimal_phase6_state(tmp_path: Path) -> Path:
    """Estado mínimo que pasa el gate 6 sin needs_human."""
    state = tmp_path / "state"
    state.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    (state / "nodes").mkdir()
    (state / "nodes_index.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
    (state / "sas_python_mapping.json").write_text(
        json.dumps({"mappings": []}), encoding="utf-8"
    )
    nb = {
        "cells": [
            {"cell_type": "code", "source": ["df = df.merge(df)\n"]},
        ]
    }
    (output / "NB-01_x.ipynb").write_text(json.dumps(nb), encoding="utf-8")
    (output / "run_all.py").write_text("NOTEBOOKS = ['NB-01_x.ipynb']\n", encoding="utf-8")
    return state


def test_unresolved_item_blocks_its_phase_gate(tmp_path: Path) -> None:
    state = _minimal_phase6_state(tmp_path)
    passed_before, _ = check_gate(6, state)
    assert passed_before

    item = needs_human.record(
        state, phase=6, task="translation", node_id="CodeTask-1",
        reason="validation_retries_exhausted", attempts=3,
    )
    assert item.id == "NH-001"

    passed, errors = check_gate(6, state)
    assert not passed
    assert any("NH-001" in e and "translation/CodeTask-1" in e for e in errors)


def test_resolved_item_unblocks(tmp_path: Path) -> None:
    state = _minimal_phase6_state(tmp_path)
    needs_human.record(state, phase=6, task="translation", reason="refusal")

    doc = yaml.safe_load((state / "needs_human.yaml").read_text(encoding="utf-8"))
    doc["items"][0]["resolved"] = True
    doc["items"][0]["resolution"] = "traducido a mano"
    (state / "needs_human.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")

    passed, errors = check_gate(6, state)
    assert passed, errors


def test_items_only_block_their_own_phase(tmp_path: Path) -> None:
    state = _minimal_phase6_state(tmp_path)
    needs_human.record(state, phase=2, task="analysis_reviews", reason="refusal")

    passed, _ = check_gate(6, state)
    assert passed, "un item de fase 2 no bloquea el gate 6"
    assert [i.id for i in needs_human.unresolved(state, phase=2)] == ["NH-001"]


def test_ids_are_sequential(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    a = needs_human.record(state, phase=2, task="a", reason="refusal")
    b = needs_human.record(state, phase=3, task="b", reason="refusal")
    assert (a.id, b.id) == ("NH-001", "NH-002")
