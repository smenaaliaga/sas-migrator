"""DoD Etapa 5: end-to-end contra una BD de prueba.

sqlite (siempre, local y CI): pipeline completo no-stub con caller fake —
B4b confirma la BD, verify_tables encuentra la tabla destino pre-existente
(el sistema no crea tablas), la ejecución autorizada corre los notebooks que
escriben en la BD, la cascada valida contra la referencia SAS y la iteración
cierra su ciclo con validación PASS.

LocalDB (SQL Server real, opcional): corre en CI windows-latest; se salta
donde no esté instalado.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest
import yaml
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import create_engine

from sas_migrator.core.config import ProjectConfig
from sas_migrator.core.db.verify_tables import verify
from sas_migrator.graph.builder import build_graph, initial_state
from sas_migrator.llm import runtime
from sas_migrator.testing.egp_builder import build_egp
from sas_migrator.testing.fake_llm import default_fake_caller, fake_translation

RESUMEN = pd.DataFrame({"region": ["N", "S"], "monto": [10.0, 20.0]})


@pytest.fixture(autouse=True)
def _fake_llm():
    caller = default_fake_caller()

    def translation(user: str):
        head = json.loads(user.splitlines()[0])
        if head["node_id"] == "CodeTask-9":  # el nodo que escribe a la BD
            nt = fake_translation(user)
            return nt.model_copy(update={"cells": [
                "datos = pd.DataFrame({'region': ['N', 'S'], 'monto': [10.0, 20.0]})\n"
                "with engine.begin() as conn:\n"
                "    conn.exec_driver_sql('DELETE FROM RESUMEN_REGIONAL')\n"
                "datos.to_sql('RESUMEN_REGIONAL', engine, index=False, if_exists='append')\n"
            ]})
        return fake_translation(user)

    caller.responses["translation"] = translation
    runtime.set_caller(caller)
    yield
    runtime.set_caller(None)


def _testdb_workspace(tmp_path: Path) -> tuple[Path, Path, str]:
    ws = tmp_path / "ws"
    (ws / "input" / "egp").mkdir(parents=True)
    (ws / "input" / "data").mkdir()
    (ws / "input" / "docs").mkdir()
    egp = build_egp(ws / "input" / "egp" / "demo.egp", db_write_task=True)

    url = f"sqlite:///{(tmp_path / 'testdb.sqlite').as_posix()}"
    (ws / "project_config.yaml").write_text(
        yaml.safe_dump({"db": {"connection_url": url}}), encoding="utf-8"
    )
    # La tabla DESTINO pre-existe (el sistema no crea tablas); vacía.
    engine = create_engine(url)
    RESUMEN.head(0).to_sql("RESUMEN_REGIONAL", engine, index=False)
    # Referencia SAS (ground truth) que la cascada comparará.
    RESUMEN.to_csv(ws / "input" / "data" / "RESUMEN_REGIONAL.csv", sep=";", index=False)
    return ws, egp, url


def _answers(payload: dict) -> dict:
    if payload["card_id"] == "execution_approval":
        return {"card_id": "execution_approval",
                "answers": [{"question_id": "Q-EXEC-1", "value": "Autorizar ejecución"}],
                "free_text": ""}
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


def test_e2e_full_pipeline_against_test_db(tmp_path: Path) -> None:
    ws, egp, url = _testdb_workspace(tmp_path)
    graph = build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "dod"}}

    result = graph.invoke(initial_state(ws, egp, stub_mode=False), config)
    steps = 0
    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        result = graph.invoke(Command(resume=_answers(payload)), config)
        steps += 1
        assert steps < 50

    assert result["done"] is True, result.get("notes")
    st = ws / "state"

    # Paso 4 B4b: la tabla destino existía y quedó verificada.
    verification = json.loads(
        (st / "table_verification.json").read_text(encoding="utf-8")
    )
    assert verification["status"] == "ok"

    # Ejecución autorizada: los notebooks corrieron y escribieron en la BD.
    exec_report = json.loads((st / "execution_report.json").read_text(encoding="utf-8"))
    assert exec_report["failed"] == 0 and exec_report["passed"] == exec_report["total"]
    engine = create_engine(url)
    escrito = pd.read_sql("SELECT * FROM RESUMEN_REGIONAL", engine)
    assert len(escrito) == 2

    # Cascada full contra la referencia SAS: PASS.
    validation = json.loads((st / "validation_report.json").read_text(encoding="utf-8"))
    assert validation["validation_mode"] == "full"
    assert validation["passed"] == 1 and validation["failed"] == 0
    assert validation["overall_status"] == "PASS"

    # Iteración post-migración: el ciclo cierra con validación re-corrida PASS.
    from sas_migrator.graph.iteration import run_iteration

    iteration = run_iteration(ws, "sin cambios, re-validar", affected_nodes=[])
    assert iteration["done"] is True, iteration["errors"]
    log = json.loads((st / "iteration_log.json").read_text(encoding="utf-8"))
    assert log["iterations"][0]["validation_result"] == "PASS"


# ── LocalDB (SQL Server real) — opcional, corre en CI windows-latest ────────

def _localdb_url() -> str | None:
    if shutil.which("sqllocaldb") is None:
        return None
    subprocess.run(["sqllocaldb", "start", "MSSQLLocalDB"], capture_output=True)
    for driver in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
        url = (
            "mssql+pyodbc://@(localdb)\\MSSQLLocalDB/tempdb"
            f"?driver={driver.replace(' ', '+')}&trusted_connection=yes"
            "&TrustServerCertificate=yes"
        )
        try:
            engine = create_engine(url)
            with engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            return url
        except Exception:
            continue
    return None


@pytest.mark.skipif(shutil.which("sqllocaldb") is None,
                    reason="SQL Server LocalDB no disponible")
def test_verify_and_cascade_against_localdb(tmp_path: Path) -> None:
    url = _localdb_url()
    if url is None:
        pytest.skip("LocalDB instalado pero no conectable")

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "IF OBJECT_ID('dbo.SASMIG_TEST', 'U') IS NOT NULL DROP TABLE dbo.SASMIG_TEST"
        )
    RESUMEN.to_sql("SASMIG_TEST", engine, schema="dbo", index=False)
    try:
        cfg = ProjectConfig.model_validate({"db": {"connection_url": url}})
        state = tmp_path / "state"
        state.mkdir()
        (state / "db_connections.yaml").write_text(
            yaml.safe_dump({"connections": [{
                "alias": "GG", "database": "tempdb", "schema_name": "dbo",
                "role": "target", "tables": ["SASMIG_TEST"],
            }]}),
            encoding="utf-8",
        )
        report, code = verify(state, cfg)
        assert code == 0 and report["status"] == "ok", report

        (state / "translation_plan.json").write_text(
            json.dumps({"targets": [{"node_id": "N", "output_tables": ["GG.SASMIG_TEST"]}]}),
            encoding="utf-8",
        )
        ref_dir = state / "reference_outputs"
        ref_dir.mkdir()
        RESUMEN.to_csv(ref_dir / "SASMIG_TEST.csv", sep=";", index=False)

        from sas_migrator.core.validation.cascade import run_cascade

        vreport, vcode = run_cascade(state, config=cfg)
        assert vcode == 0 and vreport["passed"] == 1, vreport
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE dbo.SASMIG_TEST")
