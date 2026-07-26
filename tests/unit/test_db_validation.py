"""Capa DB dialect-agnóstica: verify_tables y cascada contra una BD de prueba
(sqlite vía db.connection_url) — los modos not_applicable/blocked/full
conservan sus exit codes semánticos."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import yaml
from sqlalchemy import create_engine

from sas_migrator.core.config import ProjectConfig
from sas_migrator.core.db.engine import build_engine, qualified_table
from sas_migrator.core.db.verify_tables import verify
from sas_migrator.core.validation.cascade import run_cascade


def _config(tmp_path: Path) -> ProjectConfig:
    url = f"sqlite:///{(tmp_path / 'testdb.sqlite').as_posix()}"
    return ProjectConfig.model_validate({"db": {"connection_url": url}})


def _seed_table(cfg: ProjectConfig, name: str, df: pd.DataFrame) -> None:
    engine = create_engine(cfg.db.connection_url)
    df.to_sql(name, engine, index=False, if_exists="replace")


def _state(tmp_path: Path, *, connections: list[dict],
           output_tables: dict[str, list[str]] | None = None) -> Path:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "db_connections.yaml").write_text(
        yaml.safe_dump({"connections": connections}), encoding="utf-8"
    )
    if output_tables is not None:
        plan = {
            "targets": [
                {"node_id": nid, "output_tables": tables}
                for nid, tables in output_tables.items()
            ]
        }
        (state / "translation_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    return state


# ── engine ──────────────────────────────────────────────────────────────────

def test_connection_url_overrides_mssql_construction(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    # sin server ni default_server: con connection_url NO debe explotar
    engine = build_engine({"alias": "X"}, cfg)
    assert engine.dialect.name == "sqlite"


def test_qualified_table_by_dialect(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    engine = build_engine({}, cfg)
    # sqlite: sin schema aunque la conexión declare dbo
    assert "dbo" not in qualified_table(engine, {"schema_name": "dbo"}, "VENTAS")


# ── verify_tables ───────────────────────────────────────────────────────────

def test_verify_ok_and_blocking_missing_targets(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _seed_table(cfg, "VENTAS", pd.DataFrame({"a": [1]}))

    state = _state(tmp_path, connections=[{
        "alias": "GG", "database": "TESTDB", "schema_name": "dbo",
        "role": "both", "tables": ["VENTAS", "RESUMEN"],
    }])
    report, code = verify(state, cfg)

    assert code == 2, "tabla DESTINO faltante es bloqueante"
    assert report["status"] == "blocked_missing_targets"
    assert [r["qualified_name"] for r in report["exists"]] == ["TESTDB.dbo.VENTAS"]
    assert (state / "table_verification.json").exists()

    _seed_table(cfg, "RESUMEN", pd.DataFrame({"b": [1]}))
    report, code = verify(state, cfg)
    assert code == 0 and report["status"] == "ok"


def test_verify_missing_source_is_not_blocking(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    state = _state(tmp_path, connections=[{
        "alias": "SRC", "database": "TESTDB", "role": "source", "tables": ["NOEXISTE"],
    }])
    report, code = verify(state, cfg)
    assert code == 0 and report["status"] == "missing_sources"


def test_verify_blocked_without_db_access(tmp_path: Path) -> None:
    cfg = ProjectConfig.model_validate(
        {"db": {"connection_url": "sqlite:///Z:/no/existe/db.sqlite"}}
    )
    state = _state(tmp_path, connections=[{"alias": "X", "role": "target", "tables": ["T"]}])
    report, code = verify(state, cfg)
    assert code == 3 and report["status"] == "blocked"


def test_verify_not_applicable_without_connections(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    report, code = verify(state, ProjectConfig())
    assert code == 0 and report["status"] == "not_applicable"


# ── cascada ─────────────────────────────────────────────────────────────────

def _reference(state: Path, name: str, df: pd.DataFrame) -> None:
    ref_dir = state / "reference_outputs"
    ref_dir.mkdir(exist_ok=True)
    df.to_csv(ref_dir / f"{name}.csv", sep=";", index=False)


def test_cascade_full_pass_against_test_db(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    df = pd.DataFrame({"ID": [1, 2, 3], "MONTO": [10.0, 20.0, 30.0]})
    _seed_table(cfg, "VENTAS", df)

    state = _state(
        tmp_path,
        connections=[{"alias": "GG", "database": "TESTDB", "schema_name": "dbo",
                      "role": "target", "tables": ["VENTAS"]}],
        output_tables={"CT-1": ["GG.VENTAS"]},
    )
    _reference(state, "VENTAS", df)

    report, code = run_cascade(state, config=cfg)

    assert code == 0, report
    assert report["validation_mode"] == "full"
    assert report["passed"] == 1 and report["failed"] == 0
    assert (state / "validation_report.json").exists()


def test_cascade_full_fail_detects_mismatch(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _seed_table(cfg, "VENTAS", pd.DataFrame({"ID": [1, 2], "MONTO": [10.0, 99.0]}))

    state = _state(
        tmp_path,
        connections=[{"alias": "GG", "database": "TESTDB", "schema_name": "dbo",
                      "role": "target", "tables": ["VENTAS"]}],
        output_tables={"CT-1": ["GG.VENTAS"]},
    )
    _reference(state, "VENTAS", pd.DataFrame({"ID": [1, 2], "MONTO": [10.0, 20.0]}))

    report, code = run_cascade(state, config=cfg)

    assert code == 1
    assert report["failed"] == 1
    result = report["results"][0]
    assert result["overall_status"] == "FAIL"
    assert any(not t["passed"] for t in result["tests"])


def test_cascade_not_applicable_without_references(tmp_path: Path) -> None:
    state = _state(tmp_path, connections=[], output_tables={})
    report, code = run_cascade(state, config=ProjectConfig())
    assert code == 0 and report["validation_mode"] == "not_applicable"


def test_cascade_blocked_without_connections(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _reference(state, "VENTAS", pd.DataFrame({"A": [1]}))
    report, code = run_cascade(state, config=ProjectConfig())
    assert code == 3 and report["validation_mode"] == "blocked"
