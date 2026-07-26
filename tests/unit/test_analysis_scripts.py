"""Tests de los scripts deterministas de análisis e índices.

Los scripts viven en .github/skills/ y se cargan por ruta (no son parte del
paquete sas_migrator).
"""
from __future__ import annotations

import json
import sys

import pandas as pd

import sas_migrator.core.analysis.analyze as ga
import sas_migrator.core.analysis.indexes as bi
import sas_migrator.core.validation.cascade as rv

# ── generate_analysis: detección de librefs ─────────────────────────────────

def test_db_librefs_excludes_sql_aliases_and_formats():
    code = """
    PROC SQL;
    CREATE TABLE GOBGENER.SALIDA AS
    SELECT T1.CAMPO, T2.OTRO FROM GOBGENER.EJECUCION T1
    LEFT JOIN WORK.TEMP T2 ON T1.ID = T2.ID;
    QUIT;
    FORMAT MONTO COMMAX12.2;
    """
    refs = ga.db_librefs_in_code(code)
    assert "GOBGENER.EJECUCION" in refs
    assert "GOBGENER.SALIDA" in refs
    assert not any(r.startswith("T1.") or r.startswith("T2.") for r in refs)
    assert not any(r.startswith("COMMAX") for r in refs)
    assert not any(r.startswith("WORK.") for r in refs)


def test_magic_numbers_only_when_repeated():
    nodes = {
        "n1": {"id": "n1", "code": "WHERE C = 5601"},
        "n2": {"id": "n2", "code": "WHERE C = 5601"},
        "n3": {"id": "n3", "code": "WHERE C = 5601 AND D = 777"},
    }
    magic = ga.find_repeated_magic_numbers(nodes, min_nodes=3)
    assert "5601" in magic and len(magic["5601"]) == 3
    assert "777" not in magic  # aparece en un solo nodo


def test_classification_priority_from_dag_not_labels():
    node = {
        "id": "sink-1", "label": "Nodo Cualquiera", "node_type": "PROC_SQL",
        "code": "PROC SQL; CREATE TABLE GOBGENER.FINAL AS SELECT * FROM GOBGENER.BASE; QUIT;" * 20,
        "inputs": ["GOBGENER.BASE"] * 6, "outputs": ["GOBGENER.FINAL"],
    }
    meta = ga.classify_node(node, sinks={"sink-1"})
    assert meta["migration_priority"] in ("critical", "high")


def test_evidence_is_deterministic(tmp_path):
    nodes = {
        f"n{i}": {"id": f"n{i}", "label": f"Nodo {i}", "node_type": "PROC_SQL",
                  "code": "CREATE TABLE GOBGENER.X AS SELECT CASE WHEN MES IN(1,2,3) THEN 1 ELSE 4 END AS TRIM FROM GOBGENER.EJECUCION WHERE ANO=&Year"}
        for i in range(6)
    }
    magic = ga.find_repeated_magic_numbers(nodes)
    ev1 = ga.build_evidence(nodes, [], magic)
    ev2 = ga.build_evidence(nodes, [], magic)
    ev1.pop("generated_at"); ev2.pop("generated_at")
    assert ev1 == ev2
    assert any(m["variable"] == "Year" and m["node_count"] == 6 for m in ev1["macro_variables"])
    assert any(d["libref"] == "GOBGENER" for d in ev1["db_dependencies"])


# ── build_indexes: topo_order ────────────────────────────────────────────────

def test_topo_order_respects_dependencies():
    edges = [{"source": "a", "target": "b"}, {"source": "b", "target": "c"}]
    order = bi.topo_order(["c", "b", "a"], edges)
    assert order["a"] < order["b"] < order["c"]


def test_topo_order_handles_cycles_without_crashing():
    edges = [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}]
    order = bi.topo_order(["a", "b"], edges)
    assert set(order) == {"a", "b"}


def test_build_indexes_end_to_end(tmp_path):
    state = tmp_path / "state"
    (state / "nodes").mkdir(parents=True)
    node = {
        "id": "CodeTask-1", "label": "Carga Base", "pfd_id": "PFD-1",
        "pfd_label": "Flujo Uno", "node_type": "PROC_SQL",
        "code": "LIBNAME GG ODBC; CREATE TABLE GG.OUT AS SELECT * FROM GG.IN WHERE A=&Year;",
        "inputs": ["GG.IN"], "outputs": ["GG.OUT"],
        "metadata": {"classification": "transform", "complexity": "low",
                     "migration_priority": "medium"},
    }
    (state / "nodes" / "CodeTask-1.json").write_text(json.dumps(node), encoding="utf-8")
    (state / "flow_graph.json").write_text(json.dumps({"edges": []}), encoding="utf-8")

    sys_argv = sys.argv
    try:
        sys.argv = ["build_indexes.py", "--state-dir", str(state)]
        bi.main()
    finally:
        sys.argv = sys_argv

    idx = json.loads((state / "nodes_index.json").read_text(encoding="utf-8"))
    assert idx["total_nodes"] == 1
    n = idx["nodes"][0]
    assert n["label"] == "Carga Base" and n["flags"]["has_db"] and n["flags"]["has_macro"]

    ev = json.loads((state / "db_evidence.json").read_text(encoding="utf-8"))
    gg = next(l for l in ev["librefs"] if l["libref"] == "GG")
    assert gg["source"] == "libname_statement" and gg["engine_hint"] == "ODBC"
    tables = {t["table"]: t["access"] for t in gg["tables"]}
    assert "IN" in tables and "OUT" in tables


# ── run_validation: cascada con DataFrames sintéticos ───────────────────────

def test_cascade_pass():
    ref = pd.DataFrame({"AÑO": [2026, 2026], "MONTO": [1.0, 2.0]})
    gen = ref.copy()
    assert rv.compare_schemas(ref, gen)["passed"]
    assert rv.compare_row_count(ref, gen)["passed"]
    assert rv.compare_values(ref, gen)["passed"]
    assert rv.compare_aggregates(ref, gen)["passed"]


def test_cascade_detects_value_mismatch():
    ref = pd.DataFrame({"K": ["a", "b"], "MONTO": [1.0, 2.0]})
    gen = pd.DataFrame({"K": ["a", "b"], "MONTO": [1.0, 5.0]})
    result = rv.compare_values(ref, gen)
    assert not result["passed"]
    assert any(m["column"] == "MONTO" for m in result["mismatches"])


def test_cascade_detects_missing_column():
    ref = pd.DataFrame({"A": [1], "B": [2]})
    gen = pd.DataFrame({"A": [1]})
    result = rv.compare_schemas(ref, gen)
    assert not result["passed"] and result["missing_columns"] == ["B"]


def test_load_target_tables_normalizes_names(tmp_path):
    plan = {"targets": [{"output_tables": ["GOBGENER.EJECUCION", "entidades"]},
                        {"output_tables": []}]}
    (tmp_path / "translation_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    assert rv.load_target_tables(tmp_path) == ["EJECUCION", "ENTIDADES"]
