"""Tests del audit de traducción: los nodos ignorados por el usuario no
bloquean la cobertura, y los no ignorados sin mapping sí."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import sas_migrator.core.audit as audit



def _write_state(tmp_path: Path, *, ignored: list[str]) -> None:
    state = tmp_path / "state"
    (state / "nodes").mkdir(parents=True)
    output = tmp_path / "output"
    output.mkdir()

    nodes = [
        {"id": "CodeTask-1", "label": "Carga datos"},
        {"id": "CodeTask-2", "label": "Nodo excluido"},
    ]
    (state / "nodes_index.json").write_text(
        json.dumps({"nodes": nodes}), encoding="utf-8"
    )
    for n in nodes:
        (state / "nodes" / f"{n['id']}.json").write_text(
            json.dumps({"id": n["id"], "code": "proc sort data=a; by x; run;"}),
            encoding="utf-8",
        )

    notebook = {
        "cells": [
            {"cell_type": "markdown", "source": ["## Carga datos\n"]},
            {"cell_type": "code", "source": ["df = df.sort_values('x')\n"]},
        ]
    }
    (output / "NB-01_test.ipynb").write_text(json.dumps(notebook), encoding="utf-8")

    mapping = {
        "mappings": [
            {
                "node_id": "CodeTask-1",
                "node_label": "Carga datos",
                "notebook_path": "output/NB-01_test.ipynb",
                "cell_index": 1,
            }
        ]
    }
    (state / "sas_python_mapping.json").write_text(json.dumps(mapping), encoding="utf-8")

    if ignored:
        (state / "ignored_nodes.yaml").write_text(
            "ignored_nodes:\n" + "".join(f"  - {nid}\n" for nid in ignored),
            encoding="utf-8",
        )


def _run_audit(tmp_path: Path, monkeypatch) -> dict:
    monkeypatch.chdir(tmp_path)
    sys_argv = sys.argv
    try:
        sys.argv = ["audit_node_translation.py", "--state-dir", "state", "--output-dir", "output"]
        audit.main()
    finally:
        sys.argv = sys_argv
    return json.loads(
        (tmp_path / "state" / "node_translation_audit.json").read_text(encoding="utf-8")
    )


def test_ignored_node_does_not_block_coverage(tmp_path, monkeypatch):
    _write_state(tmp_path, ignored=["CodeTask-2"])
    report = _run_audit(tmp_path, monkeypatch)

    assert report["summary"]["missing_mapping_count"] == 0
    assert report["summary"]["ignored_count"] == 1
    assert report["summary"]["nodes_in_scope"] == 1
    assert report["ignored_nodes"] == ["CodeTask-2"]


def test_unignored_node_without_mapping_blocks_coverage(tmp_path, monkeypatch):
    _write_state(tmp_path, ignored=[])
    report = _run_audit(tmp_path, monkeypatch)

    assert report["summary"]["missing_mapping_count"] == 1
    assert any(
        i["node_id"] == "CodeTask-2" and i["category"] == "coverage"
        for i in report["issues"]
    )
