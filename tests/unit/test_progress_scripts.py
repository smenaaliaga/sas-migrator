"""Tests del ledger de análisis (Fase 2) y del checkpoint de generación (Fase 6)."""

from __future__ import annotations

import json
from pathlib import Path

import sas_migrator.core.analysis.ledger as ledger
import sas_migrator.core.generation_status as genstat

# ── analysis_ledger ──────────────────────────────────────────────────────────

def _state_with_index(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    (state / "nodes_index.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {"id": "CT-1", "pfd_id": "PFD-1"},
                    {"id": "CT-2", "pfd_id": "PFD-1"},
                    {"id": "CT-3", "pfd_id": "PFD-2"},
                ]
            }
        ),
        encoding="utf-8",
    )
    return state


def _read_ledger(state: Path) -> dict:
    return json.loads((state / "analysis_progress.json").read_text(encoding="utf-8"))


def test_ledger_init_creates_pending_entries(tmp_path):
    state = _state_with_index(tmp_path)
    assert ledger.cmd_init(state) == 0

    data = _read_ledger(state)
    assert data["total_nodes"] == 3
    assert data["pending"] == 3
    assert {n["batch"] for n in data["nodes"]} == {"PFD-1", "PFD-2"}


def test_ledger_mark_and_status(tmp_path):
    state = _state_with_index(tmp_path)
    ledger.cmd_init(state)

    assert ledger.cmd_mark(state, ["CT-1", "CT-2"], "carga y orden, sin riesgos") == 0
    data = _read_ledger(state)
    assert data["reviewed"] == 2 and data["pending"] == 1
    reviewed = {n["node_id"]: n for n in data["nodes"]}
    assert reviewed["CT-1"]["note"] == "carga y orden, sin riesgos"
    assert reviewed["CT-1"]["reviewed_at"]


def test_ledger_mark_unknown_id_fails(tmp_path):
    state = _state_with_index(tmp_path)
    ledger.cmd_init(state)
    assert ledger.cmd_mark(state, ["NO-EXISTE"], "") == 1


def test_ledger_reinit_preserves_reviews(tmp_path):
    state = _state_with_index(tmp_path)
    ledger.cmd_init(state)
    ledger.cmd_mark(state, ["CT-1"], "ok")

    assert ledger.cmd_init(state) == 0  # idempotente
    data = _read_ledger(state)
    assert data["reviewed"] == 1
    assert {n["node_id"]: n["status"] for n in data["nodes"]}["CT-1"] == "reviewed"


def test_ledger_sync_from_review_files(tmp_path):
    state = _state_with_index(tmp_path)
    ledger.cmd_init(state)

    reviews = state / "analysis_reviews"
    reviews.mkdir()
    (reviews / "PFD-1.json").write_text(
        json.dumps(
            {
                "pfd_id": "PFD-1",
                "reviews": [
                    {"node_id": "CT-1", "note": "carga ventas"},
                    {"node_id": "CT-2", "note": "orden final"},
                ],
            }
        ),
        encoding="utf-8",
    )

    assert ledger.cmd_sync(state) == 0
    data = _read_ledger(state)
    assert data["reviewed"] == 2 and data["pending"] == 1
    assert {n["node_id"]: n["note"] for n in data["nodes"]}["CT-2"] == "orden final"


def test_ledger_sync_flags_unknown_ids(tmp_path):
    state = _state_with_index(tmp_path)
    ledger.cmd_init(state)

    reviews = state / "analysis_reviews"
    reviews.mkdir()
    (reviews / "PFD-9.json").write_text(
        json.dumps({"reviews": [{"node_id": "FANTASMA-1", "note": "x"}]}),
        encoding="utf-8",
    )
    assert ledger.cmd_sync(state) == 1


# ── generation_status ────────────────────────────────────────────────────────

def _phase6_state(tmp_path: Path, *, mapped: list[str], notebooks: list[str]) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "output").mkdir()

    plan = {
        "targets": [
            {"node_id": "CT-1", "notebook_path": "output/NB-01_flujo.ipynb"},
            {"node_id": "CT-2", "notebook_path": "output/NB-01_flujo.ipynb"},
            {"node_id": "CT-3", "notebook_path": "output/NB-02_otro.ipynb"},
        ],
        "ignored_nodes": [],
    }
    (state / "translation_plan.json").write_text(json.dumps(plan), encoding="utf-8")

    mapping = {
        "mappings": [
            {"node_id": nid, "notebook_path": "output/NB-01_flujo.ipynb", "cell_index": 1}
            for nid in mapped
        ]
    }
    (state / "sas_python_mapping.json").write_text(json.dumps(mapping), encoding="utf-8")

    nb = {"cells": [{"cell_type": "code", "source": ["import pandas as pd\n"]}]}
    for name in notebooks:
        (tmp_path / name).write_text(json.dumps(nb), encoding="utf-8")
    return state


def test_generation_status_reports_pending(tmp_path, monkeypatch):
    state = _phase6_state(
        tmp_path, mapped=["CT-1"], notebooks=["output/NB-01_flujo.ipynb"]
    )
    monkeypatch.chdir(tmp_path)
    result = genstat.build_status(state, tmp_path / "output")

    assert result["done"] == ["CT-1"]
    assert set(result["pending"]) == {"CT-2", "CT-3"}
    by_nb = result["by_notebook"]
    assert by_nb["output/NB-01_flujo.ipynb"]["pending"] == ["CT-2"]
    assert by_nb["output/NB-02_otro.ipynb"]["notebook_exists"] is False


def test_generation_status_all_done(tmp_path, monkeypatch):
    state = _phase6_state(
        tmp_path,
        mapped=["CT-1", "CT-2", "CT-3"],
        notebooks=["output/NB-01_flujo.ipynb", "output/NB-02_otro.ipynb"],
    )
    monkeypatch.chdir(tmp_path)
    result = genstat.build_status(state, tmp_path / "output")
    assert result["pending"] == []
    assert result["total_targets"] == 3
