"""Tests del ledger de análisis (Fase 2)."""

from __future__ import annotations

import json
from pathlib import Path

import sas_migrator.core.analysis.ledger as ledger

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
