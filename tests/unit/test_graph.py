"""Tests del grafo de migración — Etapa 1.

Protegen las cuatro garantías del DoD:
1. El pipeline corre end-to-end sobre el .egp sintético (stubs LLM).
2. Un gate fallido DETIENE el grafo: las fases posteriores no se ejecutan.
3. Determinismo: dos corridas sobre el mismo workspace (con reset entre medio)
   producen artefactos idénticos módulo timestamps.
4. Reanudación: interrumpir y reanudar vía checkpointer continúa sin repetir.

Más la frontera de arquitectura: core/ no importa graph/llm/langgraph.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from sas_migrator.graph import stubs
from sas_migrator.graph.builder import build_graph, initial_state
from sas_migrator.testing.egp_builder import build_egp


def make_workspace(root: Path) -> tuple[Path, Path]:
    ws = root / "ws"
    (ws / "input" / "egp").mkdir(parents=True)
    (ws / "input" / "data").mkdir()
    (ws / "input" / "docs").mkdir()
    egp = build_egp(ws / "input" / "egp" / "demo.egp")
    return ws, egp


# ── 1. End-to-end ────────────────────────────────────────────────────────────

def test_fase7_subnodos_y_gate_unico_camino() -> None:
    """ADR-0009: verify → authorize → execute_validate → gate7, y NINGÚN
    sub-nodo de la fase 7 tiene arista directa a la fase 8 — el gate sigue
    siendo el único camino, aunque la fase sea tres nodos."""
    g = build_graph().get_graph()
    edges = {(e.source, e.target) for e in g.edges}

    assert ("phase7_verify", "phase7_authorize") in edges
    assert ("phase7_authorize", "phase7_execute_validate") in edges
    assert ("phase7_execute_validate", "gate7") in edges
    assert not any(
        src.startswith("phase7") and dst == "phase8_docs" for src, dst in edges
    )
    assert all(src == "gate7" for src, dst in edges if dst == "phase8_docs")


def test_e2e_stub_run_completes_all_gates(tmp_path: Path) -> None:
    ws, egp = make_workspace(tmp_path)
    result = build_graph().invoke(initial_state(ws, egp))

    assert result["done"] is True
    gates = [(g["phase"], g["passed"]) for g in result["gate_history"]]
    assert gates == [(p, True) for p in range(9)]

    st = ws / "state"
    for artifact in (
        "intake.json", "initial_interview.yaml", "flow_graph.json",
        "extraction_residue.json", "nodes_index.json", "analysis_progress.json",
        "improvements_proposed.yaml", "profile_report.json", "file_mapping.json",
        "approved_improvements.yaml", "ignored_nodes.yaml", "translation_plan.json",
        "sas_python_mapping.json", "node_translation_audit.json",
        "validation_report.json", "migration_state.json",
    ):
        assert (st / artifact).exists(), f"falta {artifact}"
    assert list((ws / "output").glob("NB-*.ipynb")), "sin notebooks generados"
    assert (ws / "output" / "run_all.py").exists(), "gen_run_all debe correr en fase 6"
    assert (ws / "output" / "docs" / "README.md").exists()


def test_migration_state_is_written_by_runtime(tmp_path: Path) -> None:
    ws, egp = make_workspace(tmp_path)
    build_graph().invoke(initial_state(ws, egp))
    ms = json.loads((ws / "state" / "migration_state.json").read_text(encoding="utf-8"))
    assert ms["current_phase"] == 9
    assert ms["project_name"] == "demo"


# ── 2. Gate bloqueante ───────────────────────────────────────────────────────

def test_failed_gate_stops_pipeline(tmp_path: Path, monkeypatch) -> None:
    ws, egp = make_workspace(tmp_path)

    # Sabotaje: la entrevista inicial escribe YAML inválido contra el schema.
    def bad_interview(state_dir: Path) -> None:
        (state_dir / "initial_interview.yaml").write_text(
            "interview_type: 123\nblocks: not-a-list\n", encoding="utf-8"
        )

    monkeypatch.setattr(stubs, "stub_initial_interview", bad_interview)

    result = build_graph().invoke(initial_state(ws, egp))

    assert result["done"] is False
    assert result["last_gate"]["phase"] == 1
    assert result["last_gate"]["passed"] is False
    assert result["last_gate"]["errors"]
    # La prueba dura: la fase 2 JAMÁS corrió — no hay extracción en disco.
    assert not (ws / "state" / "flow_graph.json").exists()
    assert not (ws / "state" / "nodes").exists()


# ── 3. Determinismo ──────────────────────────────────────────────────────────

VOLATILE_KEYS = {
    "generated_at", "extracted_at", "scanned_at", "created_at", "updated_at",
    "checked_at", "answered_at", "started_at", "completed_at", "timestamp",
    "reviewed_at", "synced_at", "marked_at",
}


def _strip_volatile(value):
    if isinstance(value, dict):
        return {
            k: _strip_volatile(v) for k, v in value.items() if k not in VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_strip_volatile(v) for v in value]
    return value


def _artifact_snapshot(ws: Path) -> dict[str, object]:
    snap: dict[str, object] = {}
    for p in sorted((ws / "state").rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ws).as_posix()
        text = p.read_text(encoding="utf-8")
        if p.suffix == ".json":
            snap[rel] = _strip_volatile(json.loads(text))
        elif p.suffix in (".yaml", ".yml"):
            snap[rel] = _strip_volatile(yaml.safe_load(text))
        else:
            # Reportes humanos (.md) llevan timestamp en el texto; se normaliza.
            snap[rel] = re.sub(r"Generated at: \S+", "Generated at: <ts>", text)
    for p in sorted((ws / "output").rglob("*.ipynb")):
        snap[p.relative_to(ws).as_posix()] = json.loads(p.read_text(encoding="utf-8"))
    return snap


def test_two_runs_produce_identical_artifacts(tmp_path: Path) -> None:
    from sas_migrator.core.reset import reset_workspace

    ws, egp = make_workspace(tmp_path)

    build_graph().invoke(initial_state(ws, egp))
    first = _artifact_snapshot(ws)

    reset_workspace(ws)
    build_graph().invoke(initial_state(ws, egp))
    second = _artifact_snapshot(ws)

    assert set(first) == set(second), (
        f"artefactos distintos entre corridas: {set(first) ^ set(second)}"
    )
    diffs = [k for k in first if first[k] != second[k]]
    assert not diffs, f"artefactos no deterministas: {diffs}"


# ── 4. Reanudación con checkpointer ──────────────────────────────────────────

def test_interrupt_and_resume_with_checkpointer(tmp_path: Path) -> None:
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    ws, egp = make_workspace(tmp_path)
    conn = sqlite3.connect(str(tmp_path / "checkpoint.sqlite"), check_same_thread=False)
    saver = SqliteSaver(conn)
    graph = build_graph(checkpointer=saver)
    config = {"configurable": {"thread_id": "mig-1"}}

    # Correr hasta detenerse ANTES de la fase 5 (plan).
    graph.invoke(initial_state(ws, egp), config, interrupt_before=["phase5_plan"])
    assert (ws / "state" / "file_mapping.json").exists(), "fase 3 debió correr"
    assert not (ws / "state" / "translation_plan.json").exists(), "fase 5 no debió correr"

    # Reanudar desde el checkpoint: input None = continuar donde quedó.
    result = graph.invoke(None, config)
    assert result["done"] is True
    assert (ws / "state" / "translation_plan.json").exists()
    gates = [(g["phase"], g["passed"]) for g in result["gate_history"]]
    assert gates == [(p, True) for p in range(9)], "reanudar no debe repetir ni saltar gates"


# ── 5. Frontera de arquitectura ──────────────────────────────────────────────

FORBIDDEN_IN_CORE = (
    "langgraph",
    "anthropic",
    "sas_migrator.graph",
    "sas_migrator.llm",
    # Etapa 3: la construcción de entrevistas es core y no puede depender del
    # transporte — ni MCP ni la capa de servicio/sesión.
    "mcp",
    "sas_migrator.mcp_server",
    "sas_migrator.service",
)


def test_core_does_not_import_llm_or_graph() -> None:
    core_dir = Path(__file__).resolve().parents[2] / "src" / "sas_migrator" / "core"
    offenders: list[str] = []
    for py in core_dir.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for forbidden in FORBIDDEN_IN_CORE:
            if f"import {forbidden}" in text or f"from {forbidden}" in text:
                offenders.append(f"{py.name}: {forbidden}")
    assert not offenders, (
        "core/ debe ser determinista puro (testeable sin API key ni runtime de "
        f"grafo); imports prohibidos: {offenders}"
    )
