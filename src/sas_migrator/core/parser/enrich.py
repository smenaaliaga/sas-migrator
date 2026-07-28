"""Enriquecimiento del análisis con el parser v2 + placement.

Corre después de build_indexes (Fase 2): parsea cada nodo, clasifica su
placement con evidencia y actualiza nodes_index.json.

Nota histórica: hasta la validación con .egp reales, aquí vivía un chequeo
cruzado contra el extractor regex del v1 (parser_upgrade_report.json). Dos
proyectos productivos barridos con CERO misses del parser v2 —todos los
desacuerdos eran falsos positivos del regex— lo jubilaron; el harness de
integración (tests/integration) sigue siendo la puerta de entrada de cada
.egp nuevo.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

from sas_migrator.core.parser.placement import classify_placement, project_db_librefs
from sas_migrator.core.parser.statements import (
    NodeParse,
    parse_sas_code,
    resolve_db_engines,
)
from sas_migrator.core.utils.fsio import dump_json as _dump_json
from sas_migrator.core.utils.fsio import load_json

_load_json = partial(load_json, required=True)


def enrich_state(state_dir: Path) -> dict:
    """Parsea cada nodo, clasifica placement y actualiza nodes_index.json."""
    state_dir = Path(state_dir)
    nodes_dir = state_dir / "nodes"
    index_path = state_dir / "nodes_index.json"
    index = _load_json(index_path)
    # El workspace es el padre de state/: de ahí sale project_config.yaml
    # (mismo patrón que core.audit).
    db_engines = resolve_db_engines(state_dir.parent)

    parses: dict[str, NodeParse] = {}
    for node_file in sorted(nodes_dir.glob("*.json")):
        node = _load_json(node_file)
        parses[node["id"]] = parse_sas_code(node.get("code") or "")

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

    placements: dict[str, int] = {}
    enriched = 0
    for entry in index.get("nodes", []):
        parse = parses.get(entry["id"])
        if parse is None:
            continue
        decision = classify_placement(parse, db_libs, db_engines)
        entry["placement"] = decision.placement
        entry["placement_reasons"] = decision.reasons
        entry["macro_refs"] = parse.macro_refs
        placements[decision.placement] = placements.get(decision.placement, 0) + 1
        enriched += 1

    _dump_json(index_path, index)

    return {
        "nodes_total": enriched,
        "placements": dict(sorted(placements.items())),
        "db_librefs": sorted(db_libs),
    }
