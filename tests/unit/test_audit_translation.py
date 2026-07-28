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


# ── endpoint inferido del SAS: el traductor no puede cambiarlo ───────────────

_SAS_HTTP = (
    'FILENAME resp TEMP;\n'
    'PROC HTTP URL = "https://api.cliente.cl/ws?serie=X" METHOD = "get" OUT = resp; RUN;'
)


def test_http_to_same_host_is_clean(tmp_path, monkeypatch):
    _write_pair(
        tmp_path,
        _SAS_HTTP,
        'r = requests.get("https://api.cliente.cl/ws", params={"serie": "X"})\n',
    )
    assert _semantic_details(tmp_path, monkeypatch) == []


def test_http_to_other_host_is_flagged_without_any_config(tmp_path, monkeypatch):
    """El host sale del URL= del propio nodo: no hay nada que declarar."""
    _write_pair(
        tmp_path,
        _SAS_HTTP,
        'r = requests.get("https://otro.endpoint.com/v2", params={"serie": "X"})\n',
    )
    details = _semantic_details(tmp_path, monkeypatch)
    assert any("api.cliente.cl" in d and "endpoint" in d for d in details), details


def test_macro_built_url_infers_no_host(tmp_path, monkeypatch):
    """Sin host literal no se infiere nada: mejor callar que dar un falso positivo."""
    _write_pair(
        tmp_path,
        'PROC HTTP URL = "&base./ws?serie=X" METHOD = "get" OUT = resp; RUN;',
        'r = requests.get(f"{BASE}/ws", params={"serie": "X"})\n',
    )
    assert _semantic_details(tmp_path, monkeypatch) == []


def test_extract_http_hosts_reads_url_from_sas() -> None:
    assert audit.extract_http_hosts(_SAS_HTTP) == ["api.cliente.cl"]
    assert audit.extract_http_hosts('FILENAME f URL "https://a.cl:443/x";') == ["a.cl"]
    assert audit.extract_http_hosts('PROC HTTP URL="&base./x";') == []


def test_extract_dest_tables_ignores_work() -> None:
    code = (
        "proc append base=tablas.pib data=work.nuevo; run;\n"
        "proc sql; create table work.tmp as select * from x; quit;"
    )
    assert audit.extract_dest_tables(code) == ["tablas.pib"]


# ── la API reemplazada por un SELECT de la tabla que ella misma puebla ───────

_SAS_HTTP_TO_TABLE = (
    'PROC HTTP URL = "https://api.cliente.cl/ws?serie=X" METHOD = "get" OUT = resp; RUN;\n'
    "PROC APPEND BASE = tablas.pib DATA = work.serie FORCE; RUN;"
)


def test_http_replaced_by_read_of_own_destination_is_flagged(tmp_path, monkeypatch):
    _write_pair(
        tmp_path,
        _SAS_HTTP_TO_TABLE,
        'df = pd.read_sql("SELECT * FROM dbo.PIB", engine)\n',
    )
    details = _semantic_details(tmp_path, monkeypatch)
    assert any("tablas.pib" in d and "populates" in d for d in details), details


def test_http_kept_while_writing_its_table_is_clean(tmp_path, monkeypatch):
    _write_pair(
        tmp_path,
        _SAS_HTTP_TO_TABLE,
        'r = requests.get("https://api.cliente.cl/ws", params={"serie": "X"})\n'
        'serie.to_sql("PIB", engine, if_exists="append", index=False)\n',
    )
    assert _semantic_details(tmp_path, monkeypatch) == []
