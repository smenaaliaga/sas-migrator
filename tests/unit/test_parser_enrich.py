"""Integración del parser v2 al pipeline: placement en nodes_index sobre el
.egp sintético completo.
"""

from __future__ import annotations

import json
from pathlib import Path

from sas_migrator.core.parser.enrich import enrich_state
from sas_migrator.graph.builder import build_graph, initial_state
from sas_migrator.testing.egp_builder import build_egp


def _run(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "input" / "egp").mkdir(parents=True)
    (ws / "input" / "data").mkdir()
    (ws / "input" / "docs").mkdir()
    egp = build_egp(ws / "input" / "egp" / "demo.egp")
    result = build_graph().invoke(initial_state(ws, egp))
    assert result["done"] is True
    return ws


def test_every_index_node_has_placement_with_evidence(tmp_path: Path) -> None:
    ws = _run(tmp_path)
    index = json.loads((ws / "state" / "nodes_index.json").read_text(encoding="utf-8"))
    for node in index["nodes"]:
        assert node.get("placement") in (
            "sql_passthrough", "sql_pushdown", "pandas", "hybrid", "ambiguous", "utility",
        ), f"nodo {node['id']} sin placement"
        assert node.get("placement_reasons"), f"nodo {node['id']} sin evidencia"


def test_parser_v2_recovers_create_table_output(tmp_path: Path) -> None:
    """CodeTask-1 del .egp sintético crea WORK.VENTAS_AGG vía CREATE TABLE —
    invisible para el regex del v1, recuperado por el parser v2 (la vista v2
    es la única fuente de inputs/outputs de los nodos)."""
    ws = _run(tmp_path)
    graph = json.loads((ws / "state" / "flow_graph.json").read_text(encoding="utf-8"))
    task1 = next(n for n in graph["nodes"] if n["id"] == "CodeTask-1")
    assert "WORK.VENTAS_AGG" in task1["outputs"]


def test_enrich_uses_custom_db_engines_from_project_config(tmp_path: Path) -> None:
    """Un engine custom declarado en project_config.yaml (parser.extra_db_engines)
    debe confirmar el libref como BD sin tocar código del migrador."""
    ws = tmp_path / "ws"
    state = ws / "state"
    (state / "nodes").mkdir(parents=True)
    node = {
        "id": "CodeTask-9",
        "label": "Carga",
        "code": (
            "libname cli miengine server=x; proc sql; "
            "create table cli.r as select * from cli.v; quit;"
        ),
    }
    (state / "nodes" / "CodeTask-9.json").write_text(json.dumps(node), encoding="utf-8")
    (state / "nodes_index.json").write_text(
        json.dumps({"nodes": [{"id": "CodeTask-9"}]}), encoding="utf-8"
    )
    (ws / "project_config.yaml").write_text(
        "parser:\n  extra_db_engines: [miengine]\n", encoding="utf-8"
    )

    enrich_state(state)

    idx = json.loads((state / "nodes_index.json").read_text(encoding="utf-8"))
    assert idx["nodes"][0]["placement"] == "sql_pushdown"


def test_synthetic_placements_route_unconfirmed_librefs_to_interview(tmp_path: Path) -> None:
    """Comportamiento diseñado sobre el .egp sintético:

    - CodeTask-1 y Query-1 leen del libref SRC, que no tiene LIBNAME en ningún
      nodo → ``ambiguous`` (va a entrevista B4b; el placement no se adivina).
    - CodeTask-2 solo toca WORK → ``pandas`` (localidad SAS replicada).
    """
    ws = _run(tmp_path)
    index = json.loads((ws / "state" / "nodes_index.json").read_text(encoding="utf-8"))
    by_id = {n["id"]: n for n in index["nodes"]}

    assert by_id["CodeTask-2"]["placement"] == "pandas"
    for nid in ("CodeTask-1", "Query-1"):
        node = by_id[nid]
        assert node["placement"] == "ambiguous", f"{nid}: {node['placement']}"
        assert any("SRC" in r for r in node["placement_reasons"]), (
            "la evidencia debe nombrar el libref sin confirmar"
        )
