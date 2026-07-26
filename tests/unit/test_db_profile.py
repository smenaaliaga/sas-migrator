"""core.db.profile — deuda de Etapa 0 saldada: el perfilador de tablas ahora
es dialect-agnóstico (inspector de SQLAlchemy) y se testea contra la BD de
prueba sqlite vía db.connection_url."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine

from sas_migrator.core.config import ProjectConfig
from sas_migrator.core.db.profile import load_connections, profile_table_from_db


def _config(tmp_path: Path) -> ProjectConfig:
    url = f"sqlite:///{(tmp_path / 'testdb.sqlite').as_posix()}"
    return ProjectConfig.model_validate({"db": {"connection_url": url}})


def _seed(cfg: ProjectConfig, name: str, df: pd.DataFrame) -> None:
    engine = create_engine(cfg.db.connection_url)
    df.to_sql(name, engine, index=False, if_exists="replace")


def test_profile_table_stats(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _seed(cfg, "VENTAS", pd.DataFrame({
        "region": ["N", "S", "N", None],
        "monto": [10.0, 20.0, 10.0, 5.0],
    }))
    conn = {"alias": "GOB", "database": "testdb", "schema_name": "dbo"}

    profile = profile_table_from_db(conn, "VENTAS", cfg)

    assert profile["row_count"] == 4
    assert profile["column_count"] == 2
    by_name = {c["name"]: c for c in profile["columns"]}
    region = by_name["region"]
    assert region["null_count"] == 1
    assert region["null_pct"] == 0.25
    assert region["unique_count"] == 2
    assert sorted(region["sample_values"]) == ["N", "S"]
    assert by_name["monto"]["unique_count"] == 3
    assert profile["metadata"]["table"] == "VENTAS"


def test_profile_without_access_raises_connection_error(tmp_path: Path) -> None:
    cfg = ProjectConfig.model_validate(
        {"db": {"connection_url": "sqlite:///Z:/no/existe/db.sqlite"}}
    )
    try:
        profile_table_from_db({"alias": "X", "database": "d"}, "T", cfg)
        raise AssertionError("debía lanzar ConnectionError")
    except ConnectionError as exc:
        assert "(connection_url)" in str(exc)


def test_profile_missing_table_raises_connection_error(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _seed(cfg, "OTRA", pd.DataFrame({"a": [1]}))
    try:
        profile_table_from_db({"alias": "X", "database": "d"}, "NO_EXISTE", cfg)
        raise AssertionError("debía lanzar ConnectionError")
    except ConnectionError:
        pass


def test_load_connections(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    assert load_connections(state) == []

    (state / "db_connections.yaml").write_text(
        "connections:\n  - alias: GOB\n    database: testdb\n    tables: [VENTAS]\n",
        encoding="utf-8",
    )
    conns = load_connections(state)
    assert len(conns) == 1 and conns[0]["alias"] == "GOB"
