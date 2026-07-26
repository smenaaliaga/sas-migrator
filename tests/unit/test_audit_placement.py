"""Auditoría placement-aware: la traducción respeta dónde corre el cómputo."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from sas_migrator.core.audit import run_audit


def _state(tmp_path: Path, *, placement: str | None, cell: str,
           decisions: list[dict] | None = None) -> tuple[Path, Path]:
    state = tmp_path / "state"
    (state / "nodes").mkdir(parents=True)
    output = tmp_path / "output"
    output.mkdir()

    node = {"id": "CT-1", "label": "Nodo uno"}
    if placement is not None:
        node["placement"] = placement
    (state / "nodes_index.json").write_text(
        json.dumps({"nodes": [node]}), encoding="utf-8"
    )
    (state / "nodes" / "CT-1.json").write_text(
        json.dumps({"id": "CT-1", "code": "proc sql; create table x as select 1; quit;"}),
        encoding="utf-8",
    )
    notebook = {
        "cells": [
            {"cell_type": "markdown", "source": ["## Nodo uno\n"]},
            {"cell_type": "code", "source": [cell]},
        ]
    }
    (output / "NB-01_t.ipynb").write_text(json.dumps(notebook), encoding="utf-8")
    (state / "sas_python_mapping.json").write_text(
        json.dumps({"mappings": [{
            "node_id": "CT-1", "node_label": "Nodo uno",
            "notebook_path": "output/NB-01_t.ipynb", "cell_index": 1,
        }]}),
        encoding="utf-8",
    )
    if decisions is not None:
        (state / "placement_decisions.yaml").write_text(
            yaml.safe_dump({"decisions": decisions}), encoding="utf-8"
        )
    return state, output


def _placement_issues(state: Path) -> list[dict]:
    report = json.loads(
        (state / "node_translation_audit.json").read_text(encoding="utf-8")
    )
    return [i for i in report["issues"] if i["category"] == "placement"]


def test_pushdown_full_table_read_with_heavy_pandas_is_high(tmp_path: Path) -> None:
    state, out = _state(
        tmp_path, placement="sql_pushdown",
        cell="df = pd.read_sql('SELECT a, b FROM base.dbo.t', eng)\n"
             "res = df.groupby('a').sum()\n",
    )
    run_audit(state, out)
    issues = _placement_issues(state)
    assert issues and issues[0]["severity"] == "high"
    assert "full-table read" in issues[0]["detail"]


def test_pushdown_with_where_is_clean(tmp_path: Path) -> None:
    state, out = _state(
        tmp_path, placement="sql_pushdown",
        cell="df = pd.read_sql('SELECT a FROM t WHERE periodo = :p', eng, params=p)\n",
    )
    run_audit(state, out)
    assert _placement_issues(state) == []


def test_pandas_with_dynamic_sql_is_high(tmp_path: Path) -> None:
    state, out = _state(
        tmp_path, placement="pandas",
        cell='df = pd.read_sql(f"SELECT * FROM {tabla}", eng)\n',
    )
    run_audit(state, out)
    issues = _placement_issues(state)
    assert issues and issues[0]["severity"] == "high"
    assert "SQL dinámico" in issues[0]["detail"]


def test_hybrid_without_where_is_medium(tmp_path: Path) -> None:
    state, out = _state(
        tmp_path, placement="hybrid",
        cell="df = pd.read_sql('SELECT * FROM t', eng)\nout = df.head()\n",
    )
    run_audit(state, out)
    issues = _placement_issues(state)
    assert issues and issues[0]["severity"] == "medium"


def test_utility_with_data_io_is_low(tmp_path: Path) -> None:
    state, out = _state(
        tmp_path, placement="utility",
        cell="params = pd.read_csv('config.csv')\n",
    )
    run_audit(state, out)
    issues = _placement_issues(state)
    assert issues and issues[0]["severity"] == "low"


def test_b4b_override_wins_over_index(tmp_path: Path) -> None:
    state, out = _state(
        tmp_path, placement="ambiguous",
        cell="df = pd.read_sql('SELECT a FROM t', eng)\nres = df.merge(df)\n",
        decisions=[{"node_id": "CT-1", "placement": "sql_pushdown", "reason": "B4b"}],
    )
    run_audit(state, out)
    issues = _placement_issues(state)
    assert issues and issues[0]["severity"] == "high"


def test_node_without_placement_has_no_placement_issues(tmp_path: Path) -> None:
    state, out = _state(
        tmp_path, placement=None,
        cell="df = pd.read_sql('SELECT * FROM t', eng)\nres = df.groupby('a').sum()\n",
    )
    run_audit(state, out)
    assert _placement_issues(state) == []
