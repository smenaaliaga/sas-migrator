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

from sas_migrator.core.config import load_project_config
from sas_migrator.core.parser.placement import classify_placement, project_db_librefs
from sas_migrator.core.parser.segments import classify_segments, node_placement_from_segments
from sas_migrator.core.parser.statements import (
    NodeParse,
    parse_sas_code,
    resolve_db_engines,
)
from sas_migrator.core.utils.fsio import dump_json as _dump_json
from sas_migrator.core.utils.fsio import load_json
from sas_migrator.core.utils.node_io import load_node

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
    sql_first = (
        load_project_config(state_dir.parent).translation.default_target == "sql"
    )

    parses: dict[str, NodeParse] = {}
    codes: dict[str, str] = {}
    for node_file in sorted(nodes_dir.glob("*.json")):
        node = load_node(node_file, required=True)
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

    placements: dict[str, int] = {}
    enriched = 0
    bloques_total = 0
    for entry in index.get("nodes", []):
        parse = parses.get(entry["id"])
        if parse is None:
            continue
        # Veredicto por bloque: el nodo lleva el resumen, pero lo que la
        # traducción consume es `placement_segments` — un nodo que ingesta un
        # archivo en su primer PROC SQL no tiene por qué traducir a pandas los
        # UPDATE que le siguen.
        segments = classify_segments(
            codes[entry["id"]], db_libs, db_engines, sql_first=sql_first
        )
        if segments:
            entry["placement"] = node_placement_from_segments(segments)
            entry["placement_reasons"] = sorted(
                {r for s in segments for r in s.reasons}
            )
            entry["placement_segments"] = [
                {
                    "index": s.index,
                    "offset": s.offset,
                    "kind": s.kind,
                    "placement": s.placement,
                    "reasons": s.reasons,
                }
                for s in segments
            ]
            bloques_total += len(segments)
        else:
            decision = classify_placement(parse, db_libs, db_engines, sql_first=sql_first)
            entry["placement"] = decision.placement
            entry["placement_reasons"] = decision.reasons
        entry["macro_refs"] = parse.macro_refs
        entry["dataset_files"] = parse.dataset_files
        placements[entry["placement"]] = placements.get(entry["placement"], 0) + 1
        enriched += 1

    _dump_json(index_path, index)

    return {
        "nodes_total": enriched,
        "segments_total": bloques_total,
        "default_target": "sql" if sql_first else "pandas",
        "placements": dict(sorted(placements.items())),
        "db_librefs": sorted(db_libs),
    }
