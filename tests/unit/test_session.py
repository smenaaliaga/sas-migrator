"""MigrationSession (capa de servicio) + CLI interactiva con guion de respuestas.

El DoD de la Etapa 3: migración completa conducida desde la CLI con
entrevistas reales (LLM stub en análisis/traducción).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from sas_migrator.cli.main import app
from sas_migrator.cli.render import default_card_answers, parse_answer, render_card
from sas_migrator.service import MigrationSession, SessionStatus
from sas_migrator.testing.egp_builder import build_egp
from sas_migrator.testing.fake_llm import default_fake_caller


@pytest.fixture(autouse=True)
def _fake_llm():
    from sas_migrator.llm import runtime

    runtime.set_caller(default_fake_caller())
    yield
    runtime.set_caller(None)


def make_workspace(root: Path) -> Path:
    ws = root / "ws"
    (ws / "input" / "egp").mkdir(parents=True)
    (ws / "input" / "data").mkdir()
    (ws / "input" / "docs").mkdir()
    build_egp(ws / "input" / "egp" / "demo.egp")
    return ws


# ── Sesión ──────────────────────────────────────────────────────────────────

def test_session_stub_run_completes(tmp_path: Path) -> None:
    session = MigrationSession(make_workspace(tmp_path))
    result = session.start(stub_mode=True)
    assert result.status == SessionStatus.COMPLETED
    assert result.completed_phases == list(range(9))
    assert "✅ Fase 0 completada" in result.messages
    assert "✅ Fase 8 completada" in result.messages


def test_session_interview_flow_and_status(tmp_path: Path) -> None:
    session = MigrationSession(make_workspace(tmp_path))
    result = session.start(stub_mode=False)

    assert result.status == SessionStatus.WAITING_USER
    assert result.pending_card is not None
    assert result.pending_card.card_id == "B1-initial"

    # status()/pending() leen del checkpointer sin avanzar el grafo
    assert session.status().status == SessionStatus.WAITING_USER
    assert session.pending().card_id == "B1-initial"

    steps = 0
    while result.status == SessionStatus.WAITING_USER:
        card = result.pending_card.model_dump(mode="json")
        result = session.answer(default_card_answers(card))
        steps += 1
        assert steps < 40
    assert result.status == SessionStatus.COMPLETED
    assert session.status().status == SessionStatus.COMPLETED
    # el flujo lean informó cada fase cerrada exactamente una vez
    all_messages = result.messages  # solo el delta final; el historial completo:
    assert session.status().completed_phases == list(range(9))
    assert any("Migración base completa" in m for m in all_messages)


def test_rewind_reinicia_la_entrevista_que_resume_continuaria(tmp_path: Path) -> None:
    """Rebobinar descarta los `__resume__` de la fase; reanudar los replica."""
    session = MigrationSession(make_workspace(tmp_path))
    result = session.start(stub_mode=False)
    first_card = result.pending_card.card_id

    # Se contesta la primera tarjeta: el checkpointer ya guarda esa respuesta,
    # aunque la fase 1 todavía no escribió nada en state/.
    session.answer(default_card_answers(result.pending_card.model_dump(mode="json")))

    # Un proceso nuevo NO vuelve a esa tarjeta: la respuesta ya está guardada.
    pending_tras_responder = MigrationSession(session.workspace).status().pending_card
    assert pending_tras_responder is None or pending_tras_responder.card_id != first_card

    rewound = MigrationSession(session.workspace).rewind_to_phase(1)
    assert rewound.status == SessionStatus.WAITING_USER
    assert rewound.pending_card.card_id == first_card, "la fase 1 vuelve a la 1ª tarjeta"


def test_rewind_rechaza_fase_desconocida_y_fase_no_alcanzada(tmp_path: Path) -> None:
    session = MigrationSession(make_workspace(tmp_path))
    session.start(stub_mode=False)  # queda esperando en la fase 1

    with pytest.raises(ValueError):
        session.rewind_to_phase(99)
    with pytest.raises(LookupError):
        session.rewind_to_phase(7)  # nunca se llegó


def test_session_status_not_started(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    assert MigrationSession(ws).status().status == SessionStatus.NOT_STARTED


# ── Render lean ─────────────────────────────────────────────────────────────

def test_render_card_marks_recommended_and_numbers_options() -> None:
    card = {
        "card_id": "T",
        "title": "Prueba",
        "transition": "Vamos:",
        "validation_error": None,
        "allow_free_text": True,
        "progress": {"index": 2, "total": 5},
        "questions": [
            {
                "id": "Q-1",
                "text": "¿Opción?",
                "question_type": "single_choice",
                "options": ["a", "b"],
                "recommended_default": "b",
                "evidence": ["archivo.json: dato"],
            }
        ],
    }
    text = render_card(card)
    assert "Vamos:" in text
    assert "[2/5]" in text
    assert "1. a" in text and "2. b  (Recomendado)" in text
    assert "· archivo.json: dato" in text
    assert text.count("\n\n") <= len(card["questions"]) + 1, "sin recaps ni relleno"


def test_parse_answer_accepts_number_text_and_default() -> None:
    q = {"id": "Q", "question_type": "single_choice", "options": ["a", "b"],
         "recommended_default": "b"}
    assert parse_answer(q, "1") == "a"
    assert parse_answer(q, "B") == "b"
    assert parse_answer(q, "") == "b"  # Enter = camino recomendado
    assert parse_answer(q, "otra cosa") is None  # → texto libre (contrapropuesta)


# ── CLI end-to-end (DoD) ────────────────────────────────────────────────────

def test_cli_no_stub_with_answers_file_completes_all_phases(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    script = tmp_path / "answers.yaml"
    script.write_text(
        "default: recommended\n"
        "answers:\n"
        "  B1-initial:\n"
        "    Q-001: \"Flujo de ventas de demostración.\"\n"
        "    Q-002: \"no\"\n"
        "    Q-003: \"no\"\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["run", "--workspace", str(ws), "--no-stub", "--answers-file", str(script)],
    )
    assert result.exit_code == 0, result.output
    for phase in range(9):
        assert f"✅ Fase {phase} completada" in result.output
    assert "Migración base completa" in result.output

    # y status refleja el cierre
    status = runner.invoke(app, ["status", "--workspace", str(ws)])
    assert status.exit_code == 0
    assert "completed" in status.output


def test_cli_status_without_migration(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["status", "--workspace", str(ws)])
    assert result.exit_code == 0
    assert "Sin migración iniciada" in result.output
