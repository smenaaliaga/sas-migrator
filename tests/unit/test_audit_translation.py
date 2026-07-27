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


# ── Semántica de escritura: espejo del SAS, no invención ────────────────────

def _write_pair(tmp_path: Path, sas_code: str, py_source: str) -> None:
    state = tmp_path / "state"
    (state / "nodes").mkdir(parents=True)
    output = tmp_path / "output"
    output.mkdir()
    (state / "nodes_index.json").write_text(
        json.dumps({"nodes": [{"id": "CT-1", "label": "Escritura"}]}), encoding="utf-8"
    )
    (state / "nodes" / "CT-1.json").write_text(
        json.dumps({"id": "CT-1", "code": sas_code}), encoding="utf-8"
    )
    notebook = {
        "cells": [
            {"cell_type": "markdown", "source": ["## Escritura\n"]},
            {"cell_type": "code", "source": [py_source]},
        ]
    }
    (output / "NB-01_w.ipynb").write_text(json.dumps(notebook), encoding="utf-8")
    (state / "sas_python_mapping.json").write_text(json.dumps({"mappings": [{
        "node_id": "CT-1", "node_label": "Escritura",
        "notebook_path": "output/NB-01_w.ipynb", "cell_index": 1,
    }]}), encoding="utf-8")


def _semantic_details(tmp_path, monkeypatch) -> list[str]:
    report = _run_audit(tmp_path, monkeypatch)
    return [i["detail"] for i in report["issues"] if i["category"] == "semantic"]


def test_sas_append_with_invented_delete_is_high(tmp_path, monkeypatch):
    _write_pair(
        tmp_path,
        "proc append base=tablas.bd data=work.nuevo force; run;",
        "with engine.begin() as c:\n"
        "    c.execute(text('DELETE FROM dbo.BD'))\n"
        "nuevo.to_sql('BD', engine, if_exists='append', index=False)\n",
    )
    details = _semantic_details(tmp_path, monkeypatch)
    assert any("ACUMULA" in d for d in details), details


def test_sas_append_translated_as_append_is_clean(tmp_path, monkeypatch):
    _write_pair(
        tmp_path,
        "proc append base=tablas.bd data=work.nuevo force; run;",
        "nuevo.to_sql('BD', engine, if_exists='append', index=False)\n",
    )
    assert _semantic_details(tmp_path, monkeypatch) == []


def test_sas_replace_translated_as_bare_append_is_flagged(tmp_path, monkeypatch):
    _write_pair(
        tmp_path,
        "proc sql; create table tablas.resumen as select * from work.r; quit;",
        "r.to_sql('RESUMEN', engine, if_exists='append', index=False)\n",
    )
    details = _semantic_details(tmp_path, monkeypatch)
    assert any("REEMPLAZA" in d for d in details), details


def test_sas_replace_translated_as_delete_plus_append_is_clean(tmp_path, monkeypatch):
    _write_pair(
        tmp_path,
        "proc sql; create table tablas.resumen as select * from work.r; quit;",
        "with engine.begin() as c:\n"
        "    c.execute(text('DELETE FROM dbo.RESUMEN'))\n"
        "r.to_sql('RESUMEN', engine, if_exists='append', index=False)\n",
    )
    assert _semantic_details(tmp_path, monkeypatch) == []


# ── dominio declarado: el traductor no puede cambiar el endpoint ─────────────

_SAS_HTTP = (
    'FILENAME resp TEMP;\n'
    'PROC HTTP URL = "https://api.cliente.cl/ws?serie=X" METHOD = "get" OUT = resp; RUN;'
)


def _declare_domain(tmp_path: Path) -> None:
    (tmp_path / "state" / "audit_heuristics.yaml").write_text(
        "domain_markers: [api.cliente.cl]\n", encoding="utf-8"
    )


def test_http_to_declared_domain_is_clean(tmp_path, monkeypatch):
    _write_pair(
        tmp_path,
        _SAS_HTTP,
        'r = requests.get("https://api.cliente.cl/ws", params={"serie": "X"})\n',
    )
    _declare_domain(tmp_path)
    assert _semantic_details(tmp_path, monkeypatch) == []


def test_http_to_other_domain_is_flagged(tmp_path, monkeypatch):
    _write_pair(
        tmp_path,
        _SAS_HTTP,
        'r = requests.get("https://otro.endpoint.com/v2", params={"serie": "X"})\n',
    )
    _declare_domain(tmp_path)
    details = _semantic_details(tmp_path, monkeypatch)
    assert any("declared domain" in d for d in details), details


def test_endpoint_rule_is_inert_without_declared_markers(tmp_path, monkeypatch):
    """Sin domain_markers la regla no existe: no inventa hallazgos."""
    _write_pair(
        tmp_path,
        _SAS_HTTP,
        'r = requests.get("https://otro.endpoint.com/v2")\n',
    )
    assert _semantic_details(tmp_path, monkeypatch) == []
