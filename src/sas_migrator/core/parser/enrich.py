"""Enriquecimiento del análisis con el parser v2 + placement.

Corre después de build_indexes (Fase 2). No pisa los campos v1 de los nodos
(inputs/outputs del extractor viven en flow_graph y alimentan el residuo);
agrega la vista v2 y la decisión de placement, y deja un reporte comparativo
v1 vs v2 — la evidencia medible de qué recuperó el parser nuevo.
"""

from __future__ import annotations

import json
from pathlib import Path

from sas_migrator.core.parser.placement import classify_placement, project_db_librefs
from sas_migrator.core.parser.statements import NodeParse, parse_sas_code


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _dump_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def enrich_state(state_dir: Path) -> dict:
    """Parsea cada nodo, clasifica placement y actualiza nodes_index.json.

    Devuelve el resumen del reporte comparativo (también escrito a
    state/parser_upgrade_report.json).
    """
    state_dir = Path(state_dir)
    nodes_dir = state_dir / "nodes"
    index_path = state_dir / "nodes_index.json"
    index = _load_json(index_path)

    parses: dict[str, NodeParse] = {}
    for node_file in sorted(nodes_dir.glob("*.json")):
        node = _load_json(node_file)
        parses[node["id"]] = parse_sas_code(node.get("code") or "")

    db_libs = project_db_librefs(parses)
    # Sumar librefs ya detectados como BD por db_evidence (LIBNAME en metadata,
    # referencias calificadas confirmadas).
    db_evidence_path = state_dir / "db_evidence.json"
    if db_evidence_path.exists():
        ev = _load_json(db_evidence_path)
        for libref_entry in ev.get("librefs", []):
            name = str(libref_entry.get("libref", "")).upper()
            if name and libref_entry.get("source") == "libname_statement":
                db_libs.add(name)

    comparison: list[dict] = []
    placements: dict[str, int] = {}
    recovered_nodes = 0

    for entry in index.get("nodes", []):
        nid = entry["id"]
        parse = parses.get(nid)
        if parse is None:
            continue
        decision = classify_placement(parse, db_libs)
        entry["placement"] = decision.placement
        entry["placement_reasons"] = decision.reasons
        entry["inputs_v2"] = sorted({f"{r.libref}.{r.table}" for r in parse.inputs})
        entry["outputs_v2"] = sorted({f"{r.libref}.{r.table}" for r in parse.outputs})
        entry["macro_refs"] = parse.macro_refs
        placements[decision.placement] = placements.get(decision.placement, 0) + 1

        # comparativa contra la vista v1 del nodo (extractor por regex)
        node = _load_json(nodes_dir / f"{nid}.json")
        v1_in = {str(x).upper() for x in node.get("inputs", [])}
        v1_out = {str(x).upper() for x in node.get("outputs", [])}
        v2_in = set(entry["inputs_v2"])
        v2_out = set(entry["outputs_v2"])
        gained_in = sorted(v2_in - {x if "." in x else f"WORK.{x}" for x in v1_in})
        gained_out = sorted(v2_out - {x if "." in x else f"WORK.{x}" for x in v1_out})
        if gained_in or gained_out:
            recovered_nodes += 1
        comparison.append(
            {
                "node_id": nid,
                "v1_inputs": sorted(v1_in),
                "v1_outputs": sorted(v1_out),
                "v2_inputs": sorted(v2_in),
                "v2_outputs": sorted(v2_out),
                "recovered_inputs": gained_in,
                "recovered_outputs": gained_out,
                "placement": decision.placement,
            }
        )

    _dump_json(index_path, index)

    summary = {
        "nodes_total": len(comparison),
        "nodes_with_recovered_io": recovered_nodes,
        "placements": dict(sorted(placements.items())),
        "db_librefs": sorted(db_libs),
    }
    _dump_json(
        state_dir / "parser_upgrade_report.json",
        {"summary": summary, "nodes": comparison},
    )
    return summary
