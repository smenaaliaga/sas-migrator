"""Runners LLM de fases 2/3/6 con FakeCaller: producen artefactos que pasan
los gates REALES; NeedsHuman queda registrado y el gate bloquea (nunca
silencio)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from sas_migrator.core.analysis import ledger
from sas_migrator.core.models.translation import NodeTranslation
from sas_migrator.core.utils.needs_human import unresolved
from sas_migrator.core.utils.schema_validation import check_gate
from sas_migrator.graph.builder import build_graph, initial_state
from sas_migrator.llm import phases, runtime
from sas_migrator.llm.contracts import FileMappingBatch
from sas_migrator.llm.errors import NeedsHuman
from sas_migrator.llm.fake import FakeCaller
from sas_migrator.testing.egp_builder import build_egp
from sas_migrator.testing.fake_llm import _header
from sas_migrator.testing.fake_llm import fake_improvements as _improvements_fake
from sas_migrator.testing.fake_llm import fake_reviews as _reviews_fake
from sas_migrator.testing.fake_llm import fake_translation as _translation_fake


@pytest.fixture(autouse=True)
def _reset_caller():
    yield
    runtime.set_caller(None)


@pytest.fixture(scope="module")
def stub_ws(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("llm_base")
    ws = root / "ws"
    (ws / "input" / "egp").mkdir(parents=True)
    (ws / "input" / "data").mkdir()
    (ws / "input" / "docs").mkdir()
    egp = build_egp(ws / "input" / "egp" / "demo.egp")
    result = build_graph().invoke(initial_state(ws, egp))
    assert result["done"] is True
    return ws


@pytest.fixture()
def ws(stub_ws: Path, tmp_path: Path) -> Path:
    dst = tmp_path / "ws"
    shutil.copytree(stub_ws, dst)
    return dst




# ── Fase 2 ──────────────────────────────────────────────────────────────────

def test_run_analysis_passes_real_gate2(ws: Path) -> None:
    state = ws / "state"
    # partir de cero: sin reviews previas, descripciones vacías, ledger pending
    shutil.rmtree(state / "analysis_reviews")
    (state / "analysis_progress.json").unlink()
    summary = json.loads((state / "flow_summary.json").read_text(encoding="utf-8"))
    for flow in summary.get("flows", []):
        flow["description"] = ""
    (state / "flow_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    ledger.cmd_init(state)
    passed, _ = check_gate(2, state)
    assert not passed, "sin reviews el gate 2 debe estar rojo"

    runtime.set_caller(FakeCaller({
        "analysis_reviews": _reviews_fake, "improvements": _improvements_fake,
    }))
    counts = phases.run_analysis(state, ws)
    ledger.cmd_sync(state)

    assert counts["pfds_ok"] == counts["pfds_total"]
    passed, errors = check_gate(2, state)
    assert passed, errors
    summary = json.loads((state / "flow_summary.json").read_text(encoding="utf-8"))
    assert any("ventas regionales" in str(f.get("description")) for f in summary["flows"])
    proposed = yaml.safe_load(
        (state / "improvements_proposed.yaml").read_text(encoding="utf-8")
    )
    assert proposed["improvements"][0]["status"] == "proposed"


def test_run_analysis_needs_human_blocks_gate2(ws: Path) -> None:
    state = ws / "state"
    (state / "analysis_progress.json").unlink()
    shutil.rmtree(state / "analysis_reviews")
    ledger.cmd_init(state)

    runtime.set_caller(FakeCaller({
        "analysis_reviews": NeedsHuman(
            task="analysis_reviews", reason="validation_retries_exhausted", attempts=3
        ),
        "improvements": _improvements_fake,
    }))
    phases.run_analysis(state, ws)
    ledger.cmd_sync(state)

    items = unresolved(state, phase=2)
    assert items and items[0].task == "analysis_reviews"
    passed, errors = check_gate(2, state)
    assert not passed
    assert any("needs_human" in e for e in errors)


# ── Fase 3 ──────────────────────────────────────────────────────────────────

def _with_profile(state: Path) -> None:
    (state / "profile_report.json").write_text(
        json.dumps([{"file_path": "input/data/clientes.csv", "file_type": "csv",
                     "row_count": 10, "column_count": 2}]),
        encoding="utf-8",
    )


def test_run_matching_with_llm(ws: Path) -> None:
    state = ws / "state"
    _with_profile(state)
    runtime.set_caller(FakeCaller({
        "matching": FileMappingBatch(mappings=[{
            "file_path": "input/data/clientes.csv", "node_id": "Query-1",
            "role": "input", "confidence": 0.9,
            "reasons": ["nombre coincide con src.clientes"],
            "needs_confirmation": False,
        }]),
    }))
    counts = phases.run_matching(state, ws)
    assert counts == {"mappings": 1, "llm": True}
    doc = json.loads((state / "file_mapping.json").read_text(encoding="utf-8"))
    assert doc["mappings"][0]["node_id"] == "Query-1"
    passed, errors = check_gate(3, state)
    assert passed, errors


def test_run_matching_needs_human_fallback(ws: Path) -> None:
    state = ws / "state"
    _with_profile(state)
    runtime.set_caller(FakeCaller({
        "matching": NeedsHuman(task="matching", reason="refusal", attempts=1),
    }))
    phases.run_matching(state, ws)

    doc = json.loads((state / "file_mapping.json").read_text(encoding="utf-8"))
    assert doc["mappings"][0]["needs_confirmation"] is True
    passed, errors = check_gate(3, state)
    assert not passed and any("needs_human" in e for e in errors)


def test_run_matching_without_profiles_skips_llm(ws: Path) -> None:
    state = ws / "state"
    runtime.set_caller(FakeCaller({}))  # cualquier llamada explotaría con KeyError
    counts = phases.run_matching(state, ws)
    assert counts == {"mappings": 0, "llm": False}


# ── Fase 6 ──────────────────────────────────────────────────────────────────

def test_run_translation_passes_real_gate6(ws: Path) -> None:
    state = ws / "state"
    runtime.set_caller(FakeCaller({"translation": _translation_fake}))
    counts = phases.run_translation(state, ws / "output", ws)

    assert counts["assembly_failures"] == 0
    assert counts["translated"] == counts["targets"]
    from sas_migrator.core.gen_run_all import write_run_all

    write_run_all(ws / "output")
    passed, errors = check_gate(6, state)
    assert passed, errors
    mapping = json.loads((state / "sas_python_mapping.json").read_text(encoding="utf-8"))
    assert all(m["confidence"] == "medium" for m in mapping["mappings"])


def test_run_translation_failed_node_is_needs_human_and_gate_blocks(ws: Path) -> None:
    state = ws / "state"

    def flaky(user: str) -> NodeTranslation:
        head = _header(user)
        if head["node_id"] == "CodeTask-1":
            raise NeedsHuman(task="translation", reason="validation_retries_exhausted",
                             attempts=3)
        return _translation_fake(user)

    runtime.set_caller(FakeCaller({"translation": flaky}))
    counts = phases.run_translation(state, ws / "output", ws)
    assert counts["translated"] == counts["targets"] - 1

    items = unresolved(state, phase=6)
    assert [i.node_id for i in items] == ["CodeTask-1"]
    passed, errors = check_gate(6, state)
    assert not passed
    assert any("needs_human" in e for e in errors)
    assert any("without SAS->Python mapping" in e for e in errors), (
        "la auditoría también reporta el nodo sin mapping (doble señal)"
    )


def test_run_translation_static_failure_is_recorded(ws: Path) -> None:
    state = ws / "state"

    def bad_code(user: str) -> NodeTranslation:
        nt = _translation_fake(user)
        if _header(user)["node_id"] == "CodeTask-2":
            return nt.model_copy(update={"cells": ["df.to_parquet('x')\n"]})
        return nt

    runtime.set_caller(FakeCaller({"translation": bad_code}))
    counts = phases.run_translation(state, ws / "output", ws)
    assert counts["assembly_failures"] == 1
    items = unresolved(state, phase=6)
    assert items[0].task == "assembly" and items[0].reason == "static_check_failed"


# ── Pipeline completo con LLM fake + entrevistas ────────────────────────────

def test_full_pipeline_llm_fake_and_interviews(tmp_path: Path) -> None:
    """DoD Etapa 4: sobre el .egp sintético, con caller fake y entrevistas
    reales, el pipeline completa las 9 fases y los notebooks pasan gate 6."""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    ws = tmp_path / "ws"
    (ws / "input" / "egp").mkdir(parents=True)
    (ws / "input" / "data").mkdir()
    (ws / "input" / "docs").mkdir()
    egp = build_egp(ws / "input" / "egp" / "demo.egp")

    from sas_migrator.testing.fake_llm import default_fake_caller

    runtime.set_caller(default_fake_caller())

    graph = build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "llm-e2e"}}
    result = graph.invoke(initial_state(ws, egp, stub_mode=False), config)
    steps = 0
    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        answers = []
        for q in payload["questions"]:
            if q["question_type"] == "multi_choice":
                value = "todos"
            elif q["options"]:
                value = q.get("recommended_default") or q["options"][0]
            else:
                value = q.get("recommended_default") or "respuesta sintética"
            answers.append({"question_id": q["id"], "value": value})
        result = graph.invoke(
            Command(resume={"card_id": payload["card_id"], "answers": answers,
                            "free_text": ""}),
            config,
        )
        steps += 1
        assert steps < 40

    assert result["done"] is True
    gates = [(g["phase"], g["passed"]) for g in result["gate_history"]]
    assert gates == [(p, True) for p in range(9)]
    mapping = json.loads(
        (ws / "state" / "sas_python_mapping.json").read_text(encoding="utf-8")
    )
    assert {m["confidence"] for m in mapping["mappings"]} == {"medium"}, (
        "la traducción vino del caller fake, no del stub"
    )
