"""Entrevistas reales como interrupt() — DoD de la Etapa 3.

Cubre: pipeline completo conducido por Command(resume=...), respuesta inválida
⇒ re-interrupt con validation_error (nunca crash), rechazo del plan ⇒ gate 5
bloquea, y reanudación a mitad de entrevista sobre SqliteSaver reconstruyendo
el grafo desde cero.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from sas_migrator.graph.builder import build_graph, initial_state
from sas_migrator.testing.egp_builder import build_egp
from sas_migrator.testing.fake_llm import default_fake_caller


@pytest.fixture(autouse=True)
def _fake_llm():
    """stub_mode=False toma la rama LLM real (Etapa 4) — estos tests prueban
    las entrevistas, así que el LLM va con el caller fake determinista."""
    from sas_migrator.llm import runtime

    runtime.set_caller(default_fake_caller())
    yield
    runtime.set_caller(None)


def make_workspace(root: Path) -> tuple[Path, Path]:
    ws = root / "ws"
    (ws / "input" / "egp").mkdir(parents=True)
    (ws / "input" / "data").mkdir()
    (ws / "input" / "docs").mkdir()
    egp = build_egp(ws / "input" / "egp" / "demo.egp")
    return ws, egp


def default_answers(payload: dict) -> dict:
    """Responde una tarjeta por el camino fácil: el default recomendado."""
    answers = []
    for q in payload["questions"]:
        if q["question_type"] == "multi_choice":
            value = "todos"
        elif q["options"]:
            value = q.get("recommended_default") or q["options"][0]
        else:
            value = q.get("recommended_default") or "respuesta sintética"
        answers.append({"question_id": q["id"], "value": value})
    return {"card_id": payload["card_id"], "answers": answers, "free_text": ""}


def drive(graph, config, state, answer_fn, max_cards: int = 40):
    """Corre el grafo respondiendo cada interrupt hasta terminar."""
    result = graph.invoke(state, config)
    seen: list[dict] = []
    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        seen.append(payload)
        assert len(seen) <= max_cards, "loop de interrupts sin converger"
        result = graph.invoke(Command(resume=answer_fn(payload)), config)
    return result, seen


# ── 1. Pipeline completo con entrevistas reales ─────────────────────────────

def test_full_pipeline_driven_by_interrupts(tmp_path: Path) -> None:
    ws, egp = make_workspace(tmp_path)
    graph = build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t1"}}

    result, cards = drive(
        graph, config, initial_state(ws, egp, stub_mode=False), default_answers
    )

    assert result["done"] is True
    gates = [(g["phase"], g["passed"]) for g in result["gate_history"]]
    assert gates == [(p, True) for p in range(9)]

    card_ids = [c["card_id"] for c in cards]
    assert card_ids[0] == "B1-initial"
    assert "B2-scope:flows" in card_ids
    assert "B2-scope:native:ImportTask-1" in card_ids
    assert "B4b:step1" in card_ids
    assert "B4b:resolve:SRC" in card_ids
    assert any(cid.startswith("B5:M-") for cid in card_ids)
    assert "B6-closure" in card_ids
    assert card_ids[-1] == "plan_approval"

    st = ws / "state"
    qa = yaml.safe_load((st / "initial_interview.yaml").read_text(encoding="utf-8"))
    assert qa["interview_type"] == "initial"
    assert all("[stub]" not in str(a.get("value")) for b in qa["blocks"] for a in b["answers"])
    decisions = yaml.safe_load((st / "placement_decisions.yaml").read_text(encoding="utf-8"))
    assert decisions["confirmed_prefixes"] == []  # el default de SRC fue "No sé"


# ── 2. Respuesta inválida ⇒ re-interrupt, nunca crash ───────────────────────

def test_invalid_answer_reinterrupts_with_validation_error(tmp_path: Path) -> None:
    ws, egp = make_workspace(tmp_path)
    graph = build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t2"}}

    result = graph.invoke(initial_state(ws, egp, stub_mode=False), config)
    first = result["__interrupt__"][0].value
    assert first["card_id"] == "B1-initial"
    assert first["validation_error"] is None

    result = graph.invoke(
        Command(resume={"card_id": "B1-initial",
                        "answers": [{"question_id": "Q-999", "value": "x"}]}),
        config,
    )
    retry = result["__interrupt__"][0].value
    assert retry["card_id"] == "B1-initial"
    assert retry["validation_error"], "la tarjeta re-presentada debe explicar el error"
    assert "Q-999" in retry["validation_error"]

    # y una respuesta válida destranca
    result = graph.invoke(Command(resume=default_answers(retry)), config)
    assert result["current_phase"] >= 1 or "__interrupt__" in result


# ── 3. Rechazo del plan ⇒ gate 5 bloquea ────────────────────────────────────

def test_plan_rejection_blocks_gate5(tmp_path: Path) -> None:
    ws, egp = make_workspace(tmp_path)
    graph = build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t3"}}

    def reject_plan(payload: dict) -> dict:
        if payload["card_id"] == "plan_approval":
            return {
                "card_id": "plan_approval",
                "answers": [{"question_id": "Q-PLAN-1", "value": "Rechazar el plan"}],
                "free_text": "quiero revisar el alcance de nuevo",
            }
        return default_answers(payload)

    result, _ = drive(graph, config, initial_state(ws, egp, stub_mode=False), reject_plan)

    assert result["done"] is False
    assert (5, False) in [(g["phase"], g["passed"]) for g in result["gate_history"]]
    assert any("BLOQUEADO en gate 5" in n for n in result["notes"])


# ── 4. Reanudación a mitad de entrevista (DoD) ──────────────────────────────

def test_resume_mid_interview_across_graph_instances(tmp_path: Path) -> None:
    ws, egp = make_workspace(tmp_path)
    db_path = str(tmp_path / "checkpoint.sqlite")
    config = {"configurable": {"thread_id": "migration"}}

    from langgraph.checkpoint.sqlite import SqliteSaver

    conn1 = sqlite3.connect(db_path, check_same_thread=False)
    graph1 = build_graph(checkpointer=SqliteSaver(conn1))

    # Responder la fase 1 y las 2 primeras tarjetas de la fase 4, y cortar.
    result = graph1.invoke(initial_state(ws, egp, stub_mode=False), config)
    answered = 0
    while "__interrupt__" in result and answered < 3:
        payload = result["__interrupt__"][0].value
        result = graph1.invoke(Command(resume=default_answers(payload)), config)
        answered += 1
    assert "__interrupt__" in result, "debe quedar entrevista pendiente a mitad de fase 4"
    pending_before = result["__interrupt__"][0].value["card_id"]
    conn1.close()

    # Reconstruir el grafo desde cero sobre el mismo sqlite y continuar.
    conn2 = sqlite3.connect(db_path, check_same_thread=False)
    graph2 = build_graph(checkpointer=SqliteSaver(conn2))
    state = graph2.get_state(config)
    resumed = state.tasks[0].interrupts[0].value
    assert resumed["card_id"] == pending_before, "la tarjeta pendiente sobrevive al restart"

    result = graph2.invoke(None, config)
    assert "__interrupt__" in result
    assert result["__interrupt__"][0].value["card_id"] == pending_before

    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        result = graph2.invoke(Command(resume=default_answers(payload)), config)

    assert result["done"] is True
    gates = [(g["phase"], g["passed"]) for g in result["gate_history"]]
    assert gates == [(p, True) for p in range(9)], "reanudar no repite ni salta gates"
    conn2.close()
