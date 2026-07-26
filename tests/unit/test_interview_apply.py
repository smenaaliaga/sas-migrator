"""apply/validate de entrevistas: los artefactos escritos pasan los gates
REALES (1 y 4), y las respuestas inválidas nunca se aplican en silencio."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sas_migrator.core.interview import apply, initial, plan_approval, post_analysis
from sas_migrator.core.interview.validate import AnswerError, validate_answers
from sas_migrator.core.models.interview import Answer, CardAnswers, InterviewCard, Question
from sas_migrator.core.utils.schema_validation import check_gate
from sas_migrator.graph.builder import build_graph, initial_state
from sas_migrator.testing.egp_builder import build_egp


@pytest.fixture(scope="module")
def ws(tmp_path_factory) -> Path:
    ws = tmp_path_factory.mktemp("apply_ws")
    (ws / "input" / "egp").mkdir(parents=True)
    (ws / "input" / "data").mkdir()
    (ws / "input" / "docs").mkdir()
    egp = build_egp(ws / "input" / "egp" / "demo.egp")
    result = build_graph().invoke(initial_state(ws, egp))
    assert result["done"] is True
    return ws


def _answer(card, values: dict[str, str], free_text: str = "") -> CardAnswers:
    return CardAnswers(
        card_id=card.card_id,
        answers=[Answer(question_id=k, value=v) for k, v in values.items()],
        free_text=free_text,
    )


# ── Fase 1 ──────────────────────────────────────────────────────────────────

def test_apply_initial_passes_real_gate1(ws: Path) -> None:
    state = ws / "state"
    card = initial.build_initial_card(state)
    ans = _answer(
        card,
        {"Q-001": "Flujo de ventas regionales.", "Q-002": "no", "Q-003": "no sé"},
    )
    validate_answers(card, ans)
    apply.apply_initial(state, [(card, ans)])

    passed, errors = check_gate(1, state)
    assert passed, errors


# ── Fase 4 completa ─────────────────────────────────────────────────────────

def _collect_phase4(state: Path) -> list[tuple[InterviewCard, CardAnswers]]:
    collected: list[tuple[InterviewCard, CardAnswers]] = []

    flows = post_analysis.build_scope_flows_card(state)
    collected.append((flows, _answer(flows, {"Q-B2-1": "todos"})))

    for card in post_analysis.build_native_node_cards(state):
        collected.append(
            (card, _answer(card, {card.questions[0].id: "Traducir a mano"},
                           free_text="Importa el Excel de clientes a WORK."))
        )

    step1 = post_analysis.build_db_step1_card(state)
    collected.append(
        (step1, _answer(step1, {"Q-B4b-1": "Sí, conectar a la base de datos"}))
    )
    for card in post_analysis.build_placement_resolution_cards(state):
        collected.append(
            (card, _answer(card, {card.questions[0].id: "Es una base de datos"}))
        )
    step2 = post_analysis.build_db_connection_card(state)
    collected.append(
        (step2, _answer(step2, {
            "Q-B4b-2": "Especificar otra conexión",
            "Q-B4b-4": "Solo lectura (fuente)",
        }, free_text="server=sqldemo, base=DEMO"))
    )

    for card in post_analysis.build_improvement_cards(state):
        collected.append((card, _answer(card, {card.questions[0].id: "Rechazar"})))

    counts = apply.summarize_counts(state, collected)
    closure = post_analysis.build_closure_card(state, counts)
    collected.append(
        (closure, _answer(closure, {"Q-B6-1": "Sí, proceder al plan de traducción"}))
    )
    for card, ans in collected:
        validate_answers(card, ans)
    return collected


def test_apply_post_analysis_passes_real_gate4(ws: Path) -> None:
    state = ws / "state"
    counts = apply.apply_post_analysis(state, _collect_phase4(state))

    passed, errors = check_gate(4, state)
    assert passed, errors
    assert counts["excluded"] == 0

    # B4b produjo sus dos artefactos: conexiones y resolución de placement.
    conns = yaml.safe_load((state / "db_connections.yaml").read_text(encoding="utf-8"))
    aliases = [c["alias"] for c in conns["connections"]]
    assert aliases == ["SRC"], "el prefijo confirmado como BD genera su conexión"
    assert "CLIENTES" in conns["connections"][0]["tables"]

    decisions = yaml.safe_load(
        (state / "placement_decisions.yaml").read_text(encoding="utf-8")
    )
    assert decisions["confirmed_prefixes"] == ["SRC"]
    resolved = {d["node_id"]: d["placement"] for d in decisions["decisions"]}
    # SRC confirmado como BD: ambos nodos ambiguos re-clasifican (BD + WORK = hybrid)
    assert resolved.get("CodeTask-1") == "hybrid"
    assert resolved.get("Query-1") == "hybrid"

    # La tarea nativa a traducir conserva su descripción como translation_notes.
    import json

    node = json.loads((state / "nodes" / "ImportTask-1.json").read_text(encoding="utf-8"))
    assert "Excel" in node["translation_notes"]


def test_noise_prefix_resolves_to_local_placement(ws: Path) -> None:
    state = ws / "state"
    collected = []
    for card in post_analysis.build_placement_resolution_cards(state):
        collected.append(
            (card, _answer(card, {card.questions[0].id: "Es una ruta de archivos (no BD)"}))
        )
    for card in post_analysis.build_improvement_cards(state):
        collected.append((card, _answer(card, {card.questions[0].id: "Rechazar"})))

    apply.apply_post_analysis(state, collected)
    decisions = yaml.safe_load(
        (state / "placement_decisions.yaml").read_text(encoding="utf-8")
    )
    assert decisions["noise_prefixes"] == ["SRC"]
    resolved = {d["node_id"]: d["placement"] for d in decisions["decisions"]}
    # SRC como ruta: los nodos trabajan con archivos/WORK → pandas
    assert resolved.get("CodeTask-1") == "pandas"
    assert resolved.get("Query-1") == "pandas"


# ── Fase 5 ──────────────────────────────────────────────────────────────────

def test_plan_rejection_blocks_gate5(ws: Path) -> None:
    state = ws / "state"
    card = plan_approval.build_plan_card(state)
    ans = _answer(card, {"Q-PLAN-1": "Rechazar el plan"}, free_text="faltan nodos")
    validate_answers(card, ans)
    assert apply.apply_plan_approval(state, ans) is False

    passed, errors = check_gate(5, state)
    assert not passed and any("user_approved" in e for e in errors)

    # y aprobar lo desbloquea
    ans_ok = _answer(card, {"Q-PLAN-1": "Aprobar el plan"})
    assert apply.apply_plan_approval(state, ans_ok) is True
    passed, _ = check_gate(5, state)
    assert passed


# ── Negativos ───────────────────────────────────────────────────────────────

def _mini_card(**kwargs) -> InterviewCard:
    defaults = dict(
        card_id="T-1",
        interview_type="post_analysis",
        phase=4,
        block_id="B-test",
        title="t",
        questions=[
            Question(id="Q-1", text="¿?", question_type="single_choice",
                     options=["a", "b"], recommended_default="a")
        ],
    )
    defaults.update(kwargs)
    return InterviewCard(**defaults)


def test_invalid_option_is_rejected() -> None:
    card = _mini_card()
    with pytest.raises(AnswerError, match="no es una opción"):
        validate_answers(card, _answer(card, {"Q-1": "zzz"}))


def test_missing_required_is_rejected_unless_free_text() -> None:
    card = _mini_card()
    with pytest.raises(AnswerError, match="requeridas"):
        validate_answers(card, CardAnswers(card_id="T-1"))
    # contrapropuesta por texto libre es el único camino alternativo
    validate_answers(card, CardAnswers(card_id="T-1", free_text="propongo otra cosa"))


def test_unknown_question_and_wrong_card_are_rejected() -> None:
    card = _mini_card()
    with pytest.raises(AnswerError, match="desconocida"):
        validate_answers(card, _answer(card, {"Q-999": "a"}))
    with pytest.raises(AnswerError, match="tarjeta activa"):
        validate_answers(card, CardAnswers(card_id="OTRA"))


def test_free_text_rejected_when_not_allowed() -> None:
    card = _mini_card(allow_free_text=False)
    with pytest.raises(AnswerError, match="texto libre"):
        validate_answers(card, _answer(card, {"Q-1": "a"}, free_text="hola"))


def test_postponed_only_via_free_text() -> None:
    assert apply.decide_improvement("", "mejor postergar esta mejora") == (
        "postponed", "mejor postergar esta mejora",
    )
    with pytest.raises(ValueError, match="contrapropuesta"):
        apply.decide_improvement("", "hagamos otra cosa distinta")


def test_apply_raises_when_improvement_has_no_decision(ws: Path) -> None:
    with pytest.raises(ValueError, match="sin decisión"):
        apply.apply_post_analysis(ws / "state", [])


def test_gate4_blocks_when_improvement_left_proposed(ws: Path, tmp_path: Path) -> None:
    """Test negativo del gate: una M-xxx sin decidir bloquea aunque el archivo
    exista (el contrato duro que la entrevista real debe seguir cumpliendo)."""
    state = ws / "state"
    original = (state / "approved_improvements.yaml").read_text(encoding="utf-8")
    try:
        doc = yaml.safe_load(original)
        for item in doc.get("improvements", []):
            item["status"] = "proposed"
        (state / "approved_improvements.yaml").write_text(
            yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        passed, errors = check_gate(4, state)
        assert not passed
        assert any("M-" in e or "status" in e.lower() for e in errors), errors
    finally:
        (state / "approved_improvements.yaml").write_text(original, encoding="utf-8")
