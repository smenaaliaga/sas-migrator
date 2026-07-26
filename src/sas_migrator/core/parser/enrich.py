"""Enriquecimiento del análisis con el parser v2 + placement.

Corre después de build_indexes (Fase 2). Los inputs/outputs de los nodos ya
son la vista v2 (el extractor los deriva de parse_sas_code); aquí se agrega la
decisión de placement al índice y se deja un reporte comparativo contra el
extractor legacy por regex — la evidencia medible de qué recupera el parser
nuevo, y el chequeo cruzado de desacuerdos (algo que v1 veía y v2 no = flag
de revisión).
"""

from __future__ import annotations

import json
from pathlib import Path

from sas_migrator.core.extractors.egp import _extract_datasets_legacy
from sas_migrator.core.parser.placement import classify_placement, project_db_librefs
from sas_migrator.core.parser.statements import (
    NodeParse,
    parse_sas_code,
    resolve_db_engines,
)


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
    # El workspace es el padre de state/: de ahí sale project_config.yaml
    # (mismo patrón que core.audit).
    db_engines = resolve_db_engines(state_dir.parent)

    parses: dict[str, NodeParse] = {}
    codes: dict[str, str] = {}
    for node_file in sorted(nodes_dir.glob("*.json")):
        node = _load_json(node_file)
        codes[node["id"]] = node.get("code") or ""
        parses[node["id"]] = parse_sas_code(codes[node["id"]])

    db_libs = project_db_librefs(parses, db_engines)
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
    disagreement_nodes = 0

    for entry in index.get("nodes", []):
        nid = entry["id"]
        parse = parses.get(nid)
        if parse is None:
            continue
        decision = classify_placement(parse, db_libs, db_engines)
        entry["placement"] = decision.placement
        entry["placement_reasons"] = decision.reasons
        entry["macro_refs"] = parse.macro_refs
        placements[decision.placement] = placements.get(decision.placement, 0) + 1

        # chequeo cruzado contra el extractor legacy (regex v1), normalizado
        # al mismo formato LIB.TABLA con WORK explícito
        legacy_in, legacy_out, _ = _extract_datasets_legacy(codes.get(nid, ""))
        v1_in = {x.upper() if "." in x else f"WORK.{x.upper()}" for x in legacy_in}
        v1_out = {x.upper() if "." in x else f"WORK.{x.upper()}" for x in legacy_out}
        v2_in = {f"{r.libref}.{r.table}" for r in parse.inputs}
        v2_out = {f"{r.libref}.{r.table}" for r in parse.outputs}
        gained_in = sorted(v2_in - v1_in)
        gained_out = sorted(v2_out - v1_out)
        # Lo que el regex veía y el parser no: desacuerdo → flag de revisión.
        # (Suele ser un falso positivo del regex, p. ej. `FROM CONNECTION`.)
        lost_in = sorted(v1_in - v2_in)
        lost_out = sorted(v1_out - v2_out)
        if gained_in or gained_out:
            recovered_nodes += 1
        if lost_in or lost_out:
            disagreement_nodes += 1
        comparison.append(
            {
                "node_id": nid,
                "v1_inputs": sorted(v1_in),
                "v1_outputs": sorted(v1_out),
                "v2_inputs": sorted(v2_in),
                "v2_outputs": sorted(v2_out),
                "recovered_inputs": gained_in,
                "recovered_outputs": gained_out,
                "lost_inputs": lost_in,
                "lost_outputs": lost_out,
                "placement": decision.placement,
            }
        )

    _dump_json(index_path, index)

    summary = {
        "nodes_total": len(comparison),
        "nodes_with_recovered_io": recovered_nodes,
        "nodes_with_lost_io": disagreement_nodes,
        "placements": dict(sorted(placements.items())),
        "db_librefs": sorted(db_libs),
    }
    _dump_json(
        state_dir / "parser_upgrade_report.json",
        {"summary": summary, "nodes": comparison},
    )
    return summary
