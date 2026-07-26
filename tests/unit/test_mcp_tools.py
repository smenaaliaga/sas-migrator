"""Tools MCP como funciones — mismo camino de código que la CLI (ADR-0005)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sas_migrator.cli.render import default_card_answers
from sas_migrator.mcp_server.server import build_tools
from sas_migrator.service import MigrationSession
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


@pytest.fixture()
def tools(tmp_path: Path) -> dict:
    return build_tools(MigrationSession(make_workspace(tmp_path)))


def test_full_migration_via_mcp_tools(tools: dict) -> None:
    result = tools["start_migration"](stub_mode=False)
    assert result["status"] == "waiting_user"

    steps = 0
    while result["status"] == "waiting_user":
        card = tools["get_pending_question"]()
        assert card is not None
        if card["card_id"] == "plan_approval":
            result = tools["approve_plan"](approved=True)
        else:
            payload = default_card_answers(card)
            result = tools["answer"](
                card_id=payload["card_id"], answers=payload["answers"]
            )
        steps += 1
        assert steps < 40
    assert result["status"] == "completed"
    assert tools["status"]()["completed_phases"] == list(range(9))
    assert tools["get_pending_question"]() is None


def test_approve_plan_errors_when_not_pending(tools: dict) -> None:
    tools["start_migration"](stub_mode=False)  # pendiente: B1-initial
    result = tools["approve_plan"](approved=True)
    assert result["status"] == "error"
    assert "B1-initial" in result["message"]


def test_start_migration_without_egp_is_typed_error(tmp_path: Path) -> None:
    ws = tmp_path / "vacio"
    (ws / "input" / "egp").mkdir(parents=True)
    tools = build_tools(MigrationSession(ws))
    result = tools["start_migration"]()
    assert result["status"] == "error"
    assert ".egp" in result["message"]


def test_iterate_is_honest_noop_and_authorize_errors_when_not_pending(
    tools: dict, tmp_path: Path
) -> None:
    """iterate sigue not_available (Etapa 5 pendiente de iteración); Etapa 5:
    authorize_execution ya es real y da error tipado si la tarjeta pendiente
    no es execution_approval. Ninguna escribe nada en ese camino."""
    before = sorted(p.name for p in tmp_path.rglob("*"))
    result = tools["iterate"]("haz algo")
    assert result["status"] == "not_available"
    result = tools["authorize_execution"]()
    assert result["status"] == "error"
    assert "execution" in result["message"] or "ninguna" in result["message"]
    after = sorted(p.name for p in tmp_path.rglob("*"))
    assert before == after, "sin efectos en disco"


def test_build_server_registers_all_seven_tools(tmp_path: Path) -> None:
    mcp = pytest.importorskip("mcp")  # noqa: F841 — extra opcional
    import anyio

    from sas_migrator.mcp_server.server import build_server

    server = build_server(make_workspace(tmp_path))
    tools = anyio.run(server.list_tools)
    assert sorted(t.name for t in tools) == [
        "answer",
        "approve_plan",
        "authorize_execution",
        "get_pending_question",
        "iterate",
        "start_migration",
        "status",
    ]


def test_answers_survive_in_state_artifacts(tools: dict, tmp_path: Path) -> None:
    tools["start_migration"](stub_mode=False)
    card = tools["get_pending_question"]()
    tools["answer"](
        card_id=card["card_id"],
        answers=[
            {"question_id": "Q-001", "value": "Consolida ventas regionales."},
            {"question_id": "Q-002", "value": "no"},
            {"question_id": "Q-003", "value": "no"},
        ],
    )
    interview = (tmp_path / "ws" / "state" / "initial_interview.yaml").read_text(
        encoding="utf-8"
    )
    assert "Consolida ventas regionales." in interview
