"""Ensamblaje del grafo: fases encadenadas con gates como edges condicionales.

La garantía central de v2: avanzar de fase SIN pasar el gate es imposible por
topología. Cada fase desemboca en un router que corre check_gate() (el mismo
del v1, con sus chequeos de sustancia); si falla, el grafo termina en el nodo
`gate_blocked` con los errores registrados — no existe arista hacia la fase
siguiente que no pase por el gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from langgraph.graph import END, StateGraph

from sas_migrator.core.utils.schema_validation import check_gate
from sas_migrator.graph import nodes
from sas_migrator.graph.state import GateRecord, MigrationGraphState

# Orden de fases: (nombre de nodo, función, fase que valida su gate)
PHASES: list[tuple[str, object, int]] = [
    ("phase0_intake", nodes.phase0_intake, 0),
    ("phase1_initial_interview", nodes.phase1_initial_interview, 1),
    ("phase2_analysis", nodes.phase2_analysis, 2),
    ("phase3_profiling", nodes.phase3_profiling, 3),
    ("phase4_post_interview", nodes.phase4_post_interview, 4),
    ("phase5_plan", nodes.phase5_plan, 5),
    ("phase6_generation", nodes.phase6_generation, 6),
    ("phase7_validation", nodes.phase7_validation, 7),
    ("phase8_docs", nodes.phase8_docs, 8),
]


def _project_migration_state(state: MigrationGraphState, phase: int, passed: bool) -> None:
    """Proyecta migration_state.json a disco — lo escribe el RUNTIME, nunca un LLM.

    Es la inversión clave respecto de v1, donde el orquestador LLM redactaba
    este JSON a mano y era el único artefacto central sin validar.
    """
    from sas_migrator.core.models.state import MigrationState, Phase

    ws = Path(state["workspace"])
    current = Phase(min(phase + 1, 9)) if passed else Phase(phase)
    ms = MigrationState(
        project_name=Path(state.get("egp_file", "")).stem,
        egp_file=state.get("egp_file"),
        current_phase=current,
        output_strategy="notebook-flow",
    )
    (ws / "state" / "migration_state.json").write_text(
        ms.model_dump_json(indent=2), encoding="utf-8"
    )


def _make_gate_node(phase: int):
    """Nodo que evalúa el gate y registra el resultado en el estado del grafo."""

    def gate_node(state: MigrationGraphState) -> dict:
        passed, errors = check_gate(phase, Path(state["workspace"]) / "state")
        _project_migration_state(state, phase, passed)
        record: GateRecord = {"phase": phase, "passed": passed, "errors": errors}
        return {"last_gate": record, "gate_history": [record]}

    return gate_node


def _gate_router(state: MigrationGraphState) -> Literal["advance", "blocked"]:
    gate = state.get("last_gate") or {}
    return "advance" if gate.get("passed") else "blocked"


def gate_blocked(state: MigrationGraphState) -> dict:
    gate = state.get("last_gate") or {}
    phase = gate.get("phase", "?")
    return {
        "done": False,
        "notes": [f"BLOQUEADO en gate {phase}: {gate.get('errors', [])[:5]}"],
    }


def build_graph(checkpointer=None):
    """Construye y compila el grafo de migración."""
    g = StateGraph(MigrationGraphState)

    for name, fn, phase in PHASES:
        g.add_node(name, fn)
        g.add_node(f"gate{phase}", _make_gate_node(phase))
    g.add_node("gate_blocked", gate_blocked)
    g.add_node("finish", nodes.finish)

    g.set_entry_point(PHASES[0][0])

    for i, (name, _fn, phase) in enumerate(PHASES):
        g.add_edge(name, f"gate{phase}")
        next_node = PHASES[i + 1][0] if i + 1 < len(PHASES) else "finish"
        g.add_conditional_edges(
            f"gate{phase}",
            _gate_router,
            {"advance": next_node, "blocked": "gate_blocked"},
        )

    g.add_edge("gate_blocked", END)
    g.add_edge("finish", END)

    return g.compile(checkpointer=checkpointer)


def initial_state(workspace: Path, egp_file: Path, *, stub_mode: bool = True) -> MigrationGraphState:
    return {
        "workspace": str(workspace),
        "egp_file": str(egp_file),
        "current_phase": -1,
        "done": False,
        "last_gate": None,
        "gate_history": [],
        "stub_mode": stub_mode,
        "notes": [],
    }
