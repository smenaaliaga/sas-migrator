"""Tests de completitud del extractor: nada del .egp se pierde en silencio.

Contrato: todo TASK del project.xml se materializa como nodo; todo code.sas del
ZIP queda asociado a un nodo o reportado como residuo; las aristas hacia nodos
nativos sobreviven; state/extraction_residue.json cuadra el .egp contra lo
extraído y su ``unexplained_count`` es el criterio del gate de Fase 2.
"""

from __future__ import annotations

import json

from sas_migrator.core.extractors.egp import extract_egp
from sas_migrator.core.models.graph import NodeType


def _node(graph, node_id):
    node = graph.node_by_id(node_id)
    assert node is not None, f"nodo {node_id} no materializado"
    return node


def test_import_task_materialized(synthetic_egp):
    graph = extract_egp(synthetic_egp())
    node = _node(graph, "ImportTask-1")
    assert node.node_type == NodeType.IMPORT
    assert node.metadata.get("native_task") is True
    assert node.metadata.get("requires_manual_review") is True
    assert node.pfd_id == "PFD-1"


def test_query_node_gets_code_from_payload(synthetic_egp):
    graph = extract_egp(synthetic_egp())
    node = _node(graph, "Query-1")
    assert node.node_type == NodeType.QUERY
    assert "src.clientes" in node.code
    assert "SRC.CLIENTES" in node.inputs


def test_edges_to_native_tasks_survive(synthetic_egp):
    graph = extract_egp(synthetic_egp())
    edge_pairs = {(e.source, e.target) for e in graph.edges}
    assert ("CodeTask-1", "CodeTask-2") in edge_pairs
    assert ("CodeTask-2", "Query-1") in edge_pairs
    assert ("Query-1", "ImportTask-1") in edge_pairs


def test_clean_fixture_residue_is_zero(synthetic_egp, tmp_path):
    out = tmp_path / "state"
    extract_egp(synthetic_egp(), out)

    residue_path = out / "extraction_residue.json"
    assert residue_path.exists(), "el extractor debe persistir extraction_residue.json"
    residue = json.loads(residue_path.read_text(encoding="utf-8"))
    assert residue["unexplained_count"] == 0
    assert residue["orphan_code_entries"] == []
    assert residue["warnings"] == []


def test_orphan_code_entry_is_unexplained(synthetic_egp, tmp_path):
    out = tmp_path / "state"
    extract_egp(synthetic_egp(orphan_code_entry=True), out)

    residue = json.loads((out / "extraction_residue.json").read_text(encoding="utf-8"))
    assert residue["unexplained_count"] > 0
    assert any("CodeTask-99" in entry for entry in residue["orphan_code_entries"])


def test_codetask_without_file_is_reported_not_lost(synthetic_egp, tmp_path):
    out = tmp_path / "state"
    graph = extract_egp(synthetic_egp(codetask_without_file=True), out)

    node = _node(graph, "CodeTask-3")
    assert node.code == ""
    assert node.metadata.get("requires_manual_review") is True

    residue = json.loads((out / "extraction_residue.json").read_text(encoding="utf-8"))
    assert "CodeTask-3" in residue["tasks_without_code"]
    # Está materializado y visible → no cuenta como inexplicado.
    assert residue["unexplained_count"] == 0


def test_unknown_dependency_is_unexplained(synthetic_egp, tmp_path):
    out = tmp_path / "state"
    extract_egp(synthetic_egp(unknown_dependency=True), out)

    residue = json.loads((out / "extraction_residue.json").read_text(encoding="utf-8"))
    assert residue["unexplained_count"] > 0
    ghost_edges = [e for e in residue["dropped_edges"] if e["reason"] == "endpoint_unknown"]
    assert any(e["source"] == "GhostTask-1" for e in ghost_edges)


def test_multi_construct_metadata(synthetic_egp):
    graph = extract_egp(synthetic_egp())
    node = _node(graph, "CodeTask-1")
    constructs = node.metadata.get("sas_constructs", [])
    assert "DATA" in constructs
    assert "PROC SQL" in constructs


def test_query_without_payload_is_reported(synthetic_egp, tmp_path):
    out = tmp_path / "state"
    graph = extract_egp(synthetic_egp(include_query_payload=False), out)

    node = _node(graph, "Query-1")
    assert node.code == ""
    residue = json.loads((out / "extraction_residue.json").read_text(encoding="utf-8"))
    assert "Query-1" in residue["queries_without_payload"]
    # Materializado y forzado a decisión en la entrevista → no inexplicado.
    assert residue["unexplained_count"] == 0


# ── Re-extracción: la topología se refresca, el análisis se conserva ────────

def test_reextraction_preserves_node_enrichment(synthetic_egp, tmp_path):
    """Correr el extractor tras la Fase 2 no puede borrar el análisis.

    Sin esto, `classification` y `translation_notes` desaparecen mientras el
    ledger sigue afirmando que el nodo fue revisado, y el gate 2 pasa en falso.
    """
    out = tmp_path / "state"
    extract_egp(synthetic_egp(), out)

    node_path = out / "nodes" / "CodeTask-1.json"
    node = json.loads(node_path.read_text(encoding="utf-8"))
    node["metadata"]["classification"] = "transform"
    node["metadata"]["complexity"] = "high"
    node["metadata"]["migration_priority"] = "critical"
    node["translation_notes"] = "JOIN con CALCULATED — descomponer en merge + assign"
    node_path.write_text(json.dumps(node), encoding="utf-8")

    extract_egp(synthetic_egp(), out)

    after = json.loads(node_path.read_text(encoding="utf-8"))
    assert after["metadata"]["classification"] == "transform"
    assert after["metadata"]["complexity"] == "high"
    assert after["metadata"]["migration_priority"] == "critical"
    assert "CALCULATED" in after["translation_notes"]
    # La topología y el código sí se refrescan desde el .egp.
    assert after["code"], "el código debe seguir viniendo de la extracción"
    assert after["metadata"].get("sas_constructs"), "los constructs se recalculan"


def test_reextraction_preserves_flow_descriptions(synthetic_egp, tmp_path):
    out = tmp_path / "state"
    extract_egp(synthetic_egp(), out)

    summary_path = out / "flow_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["flows"][0]["description"] = "Consolida ventas y publica el cierre mensual."
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    extract_egp(synthetic_egp(), out)

    after = json.loads(summary_path.read_text(encoding="utf-8"))
    assert after["flows"][0]["description"] == "Consolida ventas y publica el cierre mensual."


def test_reextraction_on_empty_state_is_unaffected(synthetic_egp, tmp_path):
    """El merge no debe inventar campos cuando no hay estado previo."""
    out = tmp_path / "state"
    extract_egp(synthetic_egp(), out)

    node = json.loads((out / "nodes" / "CodeTask-1.json").read_text(encoding="utf-8"))
    assert "classification" not in node.get("metadata", {})
    assert not node.get("translation_notes")


def test_snapshot_code_entries_are_not_unexplained(synthetic_egp, tmp_path):
    """Versiones históricas bajo un prefijo de snapshot no son código perdido.

    Un .egp real trae cientos de `<snap>/PFD-x/CodeTask-y/code.sas` de tasks ya
    borradas. Contarlas como huérfanas bloquearía el gate 2 para siempre; no
    contarlas en absoluto perdería los huérfanos de verdad (los de la raíz).
    """
    import zipfile

    egp = synthetic_egp()
    with zipfile.ZipFile(egp, "a") as zf:
        zf.writestr(
            "SnapABC/PFD-1/CodeTask-viejo/code.sas", "data work.v1; x=1; run;"
        )

    out = tmp_path / "state"
    extract_egp(egp, out)
    residue = json.loads((out / "extraction_residue.json").read_text(encoding="utf-8"))

    assert residue["unexplained_count"] == 0
    assert residue["orphan_code_entries"] == []
    assert any("CodeTask-viejo" in e for e in residue["snapshot_code_entries"])
