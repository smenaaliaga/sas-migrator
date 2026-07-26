"""Fase 7 (diagnóstico LLM de mismatches + gate de validación) y fase 8
(doc-writer)."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
import yaml
from sqlalchemy import create_engine

from sas_migrator.core.config import ProjectConfig
from sas_migrator.core.utils.needs_human import unresolved
from sas_migrator.core.utils.schema_validation import check_gate
from sas_migrator.core.validation.cascade import run_cascade
from sas_migrator.llm import phases, runtime
from sas_migrator.llm.errors import NeedsHuman
from sas_migrator.llm.fake import FakeCaller
from sas_migrator.testing.fake_llm import fake_diagnoses, fake_docs


@pytest.fixture(autouse=True)
def _reset_caller():
    yield
    runtime.set_caller(None)


def _mismatch_state(tmp_path: Path) -> tuple[Path, ProjectConfig]:
    """Workspace con BD sqlite, referencia que NO coincide y conexiones B4b."""
    ws = tmp_path / "ws"
    state = ws / "state"
    state.mkdir(parents=True)
    url = f"sqlite:///{(ws / 'testdb.sqlite').as_posix()}"
    (ws / "project_config.yaml").write_text(
        yaml.safe_dump({"db": {"connection_url": url}}), encoding="utf-8"
    )
    cfg = ProjectConfig.model_validate({"db": {"connection_url": url}})

    engine = create_engine(url)
    pd.DataFrame({"ID": [1, 2], "MONTO": [10.0, 99.0]}).to_sql(
        "VENTAS", engine, index=False
    )
    (state / "db_connections.yaml").write_text(
        yaml.safe_dump({"connections": [{
            "alias": "GG", "database": "TESTDB", "schema_name": "dbo",
            "role": "target", "tables": ["VENTAS"],
        }]}),
        encoding="utf-8",
    )
    (state / "translation_plan.json").write_text(
        json.dumps({"targets": [{"node_id": "CT-1", "output_tables": ["GG.VENTAS"]}]}),
        encoding="utf-8",
    )
    ref_dir = state / "reference_outputs"
    ref_dir.mkdir()
    pd.DataFrame({"ID": [1, 2], "MONTO": [10.0, 20.0]}).to_csv(
        ref_dir / "VENTAS.csv", sep=";", index=False
    )
    return ws, cfg


def test_mismatch_diagnosis_with_the_8_patterns(tmp_path: Path) -> None:
    ws, cfg = _mismatch_state(tmp_path)
    state = ws / "state"
    report, code = run_cascade(state, config=cfg)
    assert code == 1 and report["failed"] == 1

    runtime.set_caller(FakeCaller({"mismatch_diagnosis": fake_diagnoses}))
    diagnoses = phases.run_mismatch_diagnosis(state, ws, report)

    assert diagnoses and diagnoses[0]["probable_cause"] == "rounding"
    assert diagnoses[0]["proposed_fix"]


def test_diagnosis_needs_human_recorded(tmp_path: Path) -> None:
    ws, cfg = _mismatch_state(tmp_path)
    state = ws / "state"
    report, _ = run_cascade(state, config=cfg)

    runtime.set_caller(FakeCaller({
        "mismatch_diagnosis": NeedsHuman(task="mismatch_diagnosis", reason="refusal",
                                         attempts=1),
    }))
    assert phases.run_mismatch_diagnosis(state, ws, report) == []
    assert [i.task for i in unresolved(state, phase=7)] == ["mismatch_diagnosis"]


def test_gate7_blocks_on_failed_validation(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "node_translation_audit.json").write_text(
        json.dumps({"generated_at": "2026-01-01T00:00:00Z",
                    "summary": {"issues_by_severity": {"high": 0}}, "issues": []}),
        encoding="utf-8",
    )
    (state / "validation_report.json").write_text(
        json.dumps({"validation_mode": "full", "failed": 1, "errors": 0,
                    "results": []}),
        encoding="utf-8",
    )
    passed, errors = check_gate(7, state)
    assert not passed
    assert any("Validación con 1 tabla(s) FAIL" in e for e in errors)


def test_gate7_blocks_on_blocked_validation(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "node_translation_audit.json").write_text(
        json.dumps({"generated_at": "2026-01-01T00:00:00Z",
                    "summary": {"issues_by_severity": {"high": 0}}, "issues": []}),
        encoding="utf-8",
    )
    (state / "validation_report.json").write_text(
        json.dumps({"validation_mode": "blocked", "note": "sin acceso"}),
        encoding="utf-8",
    )
    passed, errors = check_gate(7, state)
    assert not passed and any("bloqueada" in e for e in errors)


# ── doc-writer (fase 8) ─────────────────────────────────────────────────────

def test_run_docs_writes_the_five_documents(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    state = ws / "state"
    state.mkdir(parents=True)
    (state / "translation_plan.json").write_text(
        json.dumps({"project_name": "demo", "targets": []}), encoding="utf-8"
    )
    runtime.set_caller(FakeCaller({"docs": fake_docs}))

    assert phases.run_docs(state, ws / "output", ws) is True
    docs = ws / "output" / "docs"
    for name in ("README.md", "LINEAGE.md", "DECISIONS.md", "IMPROVEMENTS.md",
                 "RUNBOOK.md"):
        text = (docs / name).read_text(encoding="utf-8")
        assert text.startswith("#") and "doc-writer" in text


def test_run_docs_needs_human_falls_back_and_blocks(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    state = ws / "state"
    state.mkdir(parents=True)
    runtime.set_caller(FakeCaller({
        "docs": NeedsHuman(task="docs", reason="validation_retries_exhausted",
                           attempts=3),
    }))
    assert phases.run_docs(state, ws / "output", ws) is False
    items = unresolved(state, phase=8)
    assert items and items[0].task == "docs"
    passed, errors = check_gate(8, state)
    assert not passed and any("needs_human" in e for e in errors)
