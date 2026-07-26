"""Fase 9: iteración como sub-grafo con gate — IterationEntry obligatoria y
validación corrida como condición de cierre de ciclo."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from sas_migrator.core.utils.schema_validation import check_iteration_gate
from sas_migrator.graph.builder import build_graph, initial_state
from sas_migrator.graph.iteration import run_iteration
from sas_migrator.llm import runtime
from sas_migrator.llm.errors import NeedsHuman
from sas_migrator.testing.egp_builder import build_egp
from sas_migrator.testing.fake_llm import default_fake_caller, fake_translation


@pytest.fixture(autouse=True)
def _fake_llm():
    runtime.set_caller(default_fake_caller())
    yield
    runtime.set_caller(None)


@pytest.fixture(scope="module")
def base_ws(tmp_path_factory) -> Path:
    ws = tmp_path_factory.mktemp("iter_base") / "ws"
    (ws / "input" / "egp").mkdir(parents=True)
    (ws / "input" / "data").mkdir()
    (ws / "input" / "docs").mkdir()
    egp = build_egp(ws / "input" / "egp" / "demo.egp")
    result = build_graph().invoke(initial_state(ws, egp))
    assert result["done"] is True
    return ws


@pytest.fixture()
def ws(base_ws: Path, tmp_path: Path) -> Path:
    dst = tmp_path / "ws"
    shutil.copytree(base_ws, dst)
    return dst


def _log(ws: Path) -> dict:
    return json.loads((ws / "state" / "iteration_log.json").read_text(encoding="utf-8"))


def test_iteration_cycle_closes_with_entry_and_validation(ws: Path) -> None:
    result = run_iteration(
        ws, "usar orden estable al ordenar ventas",
        request_type="bug_fix", affected_nodes=["CodeTask-2"],
    )

    assert result["done"] is True, result["errors"]
    assert result["entry_id"] == "IT-001" and result["cycle"] == 1

    entry = _log(ws)["iterations"][0]
    assert entry["status"] == "completed"
    assert entry["validation_result"] == "WARN"  # sin referencias/BD: corrida honesta
    assert entry["affected_nodes"] == ["CodeTask-2"]
    assert entry["changes_made"], "los notebooks re-ensamblados quedan registrados"

    # la traducción re-corrida quedó persistida y ensamblada
    nt = json.loads(
        (ws / "state" / "translations" / "CodeTask-2.json").read_text(encoding="utf-8")
    )
    assert nt["confidence"] == "medium", "vino del caller fake, no del stub"


def test_second_iteration_increments_cycle(ws: Path) -> None:
    run_iteration(ws, "primera", affected_nodes=[])
    result = run_iteration(ws, "segunda", affected_nodes=[])
    assert result["cycle"] == 2 and result["entry_id"] == "IT-002"
    assert len(_log(ws)["iterations"]) == 2


def test_failed_retranslation_blocks_the_cycle_gate(ws: Path) -> None:
    def flaky(user: str) -> object:
        head = json.loads(user.splitlines()[0])
        if head.get("node_id") == "CodeTask-2":
            raise NeedsHuman(task="translation",
                             reason="validation_retries_exhausted", attempts=3)
        return fake_translation(user)

    caller = default_fake_caller()
    caller.responses["translation"] = flaky
    runtime.set_caller(caller)

    result = run_iteration(ws, "ajuste", affected_nodes=["CodeTask-2"])

    assert result["done"] is False
    assert any("needs_human" in e for e in result["errors"])
    # la entry existe igual (nunca silencio) pero el ciclo queda bloqueado
    assert _log(ws)["iterations"][0]["status"] == "completed"


def test_iteration_gate_requires_entry(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    passed, errors = check_iteration_gate(state, cycle=1)
    assert not passed
    assert any("Sin IterationEntry" in e for e in errors)


def test_session_iterate_wires_the_subgraph(ws: Path) -> None:
    from sas_migrator.service import MigrationSession

    result = MigrationSession(ws).iterate("mejora menor", affected_nodes=[])
    assert result["done"] is True and result["cycle"] == 1
