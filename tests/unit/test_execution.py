"""Ejecución de notebooks (nbclient) detrás del interrupt de autorización."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from sas_migrator.core.execution import execute_notebooks
from sas_migrator.core.models.translation import NodeTranslation
from sas_migrator.core.utils.schema_validation import check_gate
from sas_migrator.graph.builder import build_graph, initial_state
from sas_migrator.testing.egp_builder import build_egp
from sas_migrator.testing.fake_llm import default_fake_caller


@pytest.fixture(autouse=True)
def _fake_llm():
    from sas_migrator.llm import runtime

    runtime.set_caller(default_fake_caller())
    yield
    runtime.set_caller(None)


def _notebook(out: Path, name: str, cells: list[str]) -> None:
    from sas_migrator.core.assembly.notebook import assemble_notebooks

    plan = {"targets": [
        {"node_id": f"N{i}", "node_label": f"N{i}", "strategy": "pandas",
         "notebook_path": f"output/{name}"}
        for i in range(len(cells))
    ]}
    translations = {
        f"N{i}": NodeTranslation(node_id=f"N{i}", node_label=f"N{i}", cells=[c])
        for i, c in enumerate(cells)
    }
    _, failures = assemble_notebooks(plan, translations, out)
    assert failures == []


def test_execute_notebooks_pass_and_fail(tmp_path: Path) -> None:
    out = tmp_path / "output"
    _notebook(out, "NB-01_ok.ipynb", ["x = pd.DataFrame({'a': [1]})\n"])
    _notebook(out, "NB-02_bad.ipynb", ["raise RuntimeError('boom')\n"])

    report = execute_notebooks(out, ["NB-01_ok.ipynb", "NB-02_bad.ipynb"])

    by_name = {r["notebook"]: r for r in report["results"]}
    assert by_name["NB-01_ok.ipynb"]["status"] == "PASS"
    assert by_name["NB-02_bad.ipynb"]["status"] == "FAIL"
    assert "boom" in by_name["NB-02_bad.ipynb"]["error"]
    assert report["passed"] == 1 and report["failed"] == 1
    # el notebook ejecutado conserva outputs (evidencia)
    executed = json.loads((out / "NB-01_ok.ipynb").read_text(encoding="utf-8"))
    assert any(c.get("execution_count") for c in executed["cells"]
               if c["cell_type"] == "code")


def test_failed_execution_blocks_gate7(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "validation_report.json").write_text(
        json.dumps({"mode": "not_applicable", "overall_status": "PASS", "results": []}),
        encoding="utf-8",
    )
    (state / "node_translation_audit.json").write_text(
        json.dumps({"generated_at": "2026-01-01T00:00:00Z",
                    "summary": {"issues_by_severity": {"high": 0}}, "issues": []}),
        encoding="utf-8",
    )
    (state / "execution_report.json").write_text(
        json.dumps({"results": [{"notebook": "NB-01_x.ipynb", "status": "FAIL",
                                 "error": "KeyError: 'monto'"}]}),
        encoding="utf-8",
    )
    passed, errors = check_gate(7, state)
    assert not passed
    assert any("execution FAILED" in e for e in errors)


def test_db_bootstrap_cell_in_assembler(tmp_path: Path) -> None:
    from sas_migrator.core.assembly.notebook import assemble_notebooks

    plan = {"targets": [{"node_id": "A", "node_label": "A", "strategy": "pandas",
                         "notebook_path": "output/NB-01_x.ipynb"}]}
    nts = {"A": NodeTranslation(node_id="A", node_label="A", cells=["x = 1\n"])}
    assemble_notebooks(plan, nts, tmp_path / "output", db_bootstrap=True)
    nb = json.loads((tmp_path / "output" / "NB-01_x.ipynb").read_text(encoding="utf-8"))
    config = "".join(nb["cells"][1]["source"])
    assert 'sqlalchemy.create_engine(os.environ["SASMIG_DB_URL"])' in config


# ── e2e: la pausa sagrada dentro del grafo ──────────────────────────────────

def _drive(graph, config, state, answer_fn):
    result = graph.invoke(state, config)
    cards = []
    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        cards.append(payload["card_id"])
        result = graph.invoke(Command(resume=answer_fn(payload)), config)
        assert len(cards) < 40
    return result, cards


def _defaults(payload: dict) -> dict:
    answers = []
    for q in payload["questions"]:
        if q["question_type"] == "multi_choice":
            value = "todos"
        elif q["options"]:
            value = q.get("recommended_default") or q["options"][0]
        else:
            value = q.get("recommended_default") or "x"
        answers.append({"question_id": q["id"], "value": value})
    return {"card_id": payload["card_id"], "answers": answers, "free_text": ""}


def _ws(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "input" / "egp").mkdir(parents=True)
    (ws / "input" / "data").mkdir()
    (ws / "input" / "docs").mkdir()
    return ws, build_egp(ws / "input" / "egp" / "demo.egp")


def test_declined_execution_completes_without_running(tmp_path: Path) -> None:
    ws, egp = _ws(tmp_path)
    graph = build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "e1"}}
    result, cards = _drive(graph, config, initial_state(ws, egp, stub_mode=False), _defaults)

    assert result["done"] is True
    assert "execution_approval" in cards
    assert not (ws / "state" / "execution_report.json").exists(), (
        "el default recomendado NO ejecuta — la pausa es sagrada"
    )
    assert any("no autorizada" in n for n in result["notes"])


def test_authorized_execution_runs_notebooks(tmp_path: Path) -> None:
    ws, egp = _ws(tmp_path)
    graph = build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "e2"}}

    def authorize(payload: dict) -> dict:
        if payload["card_id"] == "execution_approval":
            return {"card_id": "execution_approval",
                    "answers": [{"question_id": "Q-EXEC-1",
                                 "value": "Autorizar ejecución"}],
                    "free_text": ""}
        return _defaults(payload)

    result, _ = _drive(graph, config, initial_state(ws, egp, stub_mode=False), authorize)

    assert result["done"] is True
    report = json.loads(
        (ws / "state" / "execution_report.json").read_text(encoding="utf-8")
    )
    assert report["failed"] == 0 and report["passed"] == report["total"] > 0
    validation = json.loads(
        (ws / "state" / "validation_report.json").read_text(encoding="utf-8")
    )
    assert validation["execution"]["failed"] == 0
