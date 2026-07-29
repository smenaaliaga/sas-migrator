#!/usr/bin/env python3
"""Construye índices livianos del análisis para que los agentes NO relean
los node files completos (~1.5 MB) ni flow_graph.json (~300 KB).

Produce:
- state/nodes_index.json — resumen por nodo: id, label, pfd, tipo,
  clasificación, complejidad, prioridad, loc, n_inputs/outputs, topo_order,
  db_refs y flags. (~30 KB para ~170 nodos)
- state/db_evidence.json — librefs de BD detectados desde el código
  (LIBNAME + referencias calificadas), tablas por libref con tipo de acceso,
  prefijos no verificados para confirmar en la entrevista B4b. (~3 KB)
- state/http_evidence.json — hosts HTTP leídos del URL= literal del SAS
  (PROC HTTP / FILENAME URL), para la entrevista B4c de conexiones externas.

Idempotente y regenerable en cualquier momento desde state/.

Uso: python .github/skills/egp-parsing/scripts/build_indexes.py [--state-dir state/]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path

from sas_migrator.core.http_evidence import build_http_evidence
from sas_migrator.core.parser.statements import resolve_db_engines
from sas_migrator.core.utils.fsio import dump_json
from sas_migrator.core.utils.node_io import decode_node_code

# Consolas Windows cp1252 no soportan "✓" — forzar UTF-8 en stdout.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FILE_EXTENSIONS = {"XLSX", "XLS", "CSV", "TXT", "PDF", "HTML", "SAS7BDAT"}
SQL_KEYWORDS = {"ON", "WHERE", "GROUP", "ORDER", "LEFT", "RIGHT", "INNER",
                "OUTER", "FULL", "JOIN", "AS", "USING"}

WRITE_PATTERNS = re.compile(
    r"\b(?:CREATE\s+TABLE|INSERT\s+INTO|DELETE\s+FROM|UPDATE|APPEND\s+BASE\s*=|DATA)\s+([A-Z][A-Z0-9_]{1,7})\.([A-Z0-9_]+)\b",
    re.I,
)


def load_nodes(nodes_dir: Path) -> list[dict]:
    return [
        decode_node_code(json.loads(p.read_text(encoding="utf-8")))
        for p in sorted(nodes_dir.glob("*.json"))
    ]


def topo_order(node_ids: list[str], edges: list[dict]) -> dict[str, int]:
    """Orden topológico (Kahn); los empates conservan el orden original."""
    original_pos = {nid: i for i, nid in enumerate(node_ids)}
    known = set(node_ids)
    succ: dict[str, list[str]] = defaultdict(list)
    indeg: dict[str, int] = {nid: 0 for nid in node_ids}
    for e in edges:
        s, t = e.get("source"), e.get("target")
        if s in known and t in known:
            succ[s].append(t)
            indeg[t] += 1

    ready = sorted([n for n in node_ids if indeg[n] == 0], key=original_pos.get)
    queue = deque(ready)
    order: dict[str, int] = {}
    pos = 0
    while queue:
        n = queue.popleft()
        order[n] = pos
        pos += 1
        pending = []
        for m in succ[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                pending.append(m)
        for m in sorted(pending, key=original_pos.get):
            queue.append(m)
    # Ciclos o nodos no alcanzados: al final, en orden original
    for n in sorted(known - set(order), key=original_pos.get):
        order[n] = pos
        pos += 1
    return order


def sql_aliases(code_upper: str) -> set[str]:
    aliases = set()
    for m in re.finditer(r"\b(?:FROM|JOIN)\s+[\w.&]+\s+(?:AS\s+)?([A-Z][A-Z0-9_]{0,7})\b", code_upper):
        if m.group(1) not in SQL_KEYWORDS:
            aliases.add(m.group(1))
    return aliases


def declared_libnames(code_upper: str, db_engines: frozenset[str]) -> dict[str, str]:
    """LIBNAME statements: libref → engine ('' si es de ruta/archivo)."""
    libs = {}
    for m in re.finditer(r"\bLIBNAME\s+([A-Z][A-Z0-9_]{0,7})\s+(\w+)?", code_upper):
        libref, engine = m.group(1), (m.group(2) or "")
        libs[libref] = engine if engine in db_engines else ""
    return libs


def qualified_refs(code_upper: str) -> set[tuple[str, str]]:
    """Referencias LIBREF.TABLA excluyendo WORK, aliases SQL, formatos y extensiones."""
    aliases = sql_aliases(code_upper)
    refs = set()
    for m in re.finditer(r"\b([A-Z][A-Z0-9_]{1,7})\.([A-Z0-9_]+)\b", code_upper):
        libref, member = m.group(1), m.group(2)
        if libref == "WORK" or libref in aliases:
            continue
        if member in FILE_EXTENSIONS or libref in FILE_EXTENSIONS or member.isdigit():
            continue
        refs.add((libref, member))
    return refs


def write_refs(code: str) -> set[tuple[str, str]]:
    return {(m.group(1).upper(), m.group(2).upper()) for m in WRITE_PATTERNS.finditer(code)
            if m.group(1).upper() != "WORK"}


def build(state_dir: Path | str) -> None:
    """Construye nodes_index.json y db_evidence.json en state_dir.

    API de módulo: los errores son RuntimeError (el pipeline los captura y
    reporta), nunca SystemExit — eso es asunto del wrapper CLI `main()`.
    """
    state = Path(state_dir)
    nodes = load_nodes(state / "nodes")
    if not nodes:
        raise RuntimeError(f"No hay nodos en {state / 'nodes'}")
    db_engines = resolve_db_engines(state.resolve().parent)

    flow_graph = json.loads((state / "flow_graph.json").read_text(encoding="utf-8"))
    order = topo_order([n["id"] for n in nodes], flow_graph.get("edges", []))
    now = datetime.now(UTC).isoformat()

    # Librerías declaradas en el inventario de flujos (señal adicional a LIBNAME)
    summary_libs: set[str] = set()
    flow_summary_path = state / "flow_summary.json"
    if flow_summary_path.exists():
        summary = json.loads(flow_summary_path.read_text(encoding="utf-8"))
        for flow in summary.get("flows", []):
            summary_libs.update(lib.upper() for lib in flow.get("libraries", []))

    # ── nodes_index.json ────────────────────────────────────────
    declared_db: dict[str, str] = {}   # libref → engine (de todos los LIBNAME del proyecto)
    for n in nodes:
        for libref, engine in declared_libnames(n.get("code", "").upper(), db_engines).items():
            if engine:
                declared_db[libref] = engine

    index_nodes = []
    libref_tables: dict[str, dict[str, dict]] = defaultdict(dict)  # libref → tabla → {access, node_ids}
    connect_to: list[dict] = []

    for n in nodes:
        code = n.get("code", "")
        upper = code.upper()
        meta = n.get("metadata", {})
        refs = qualified_refs(upper)
        writes = write_refs(code)

        db_refs = sorted(f"{lib}.{tab}" for lib, tab in refs)
        index_nodes.append({
            "id": n["id"],
            "label": n.get("label", ""),
            "pfd_id": n.get("pfd_id", ""),
            "pfd_label": n.get("pfd_label", ""),
            "node_type": n.get("node_type", "OTHER"),
            "classification": meta.get("classification", ""),
            "complexity": meta.get("complexity", ""),
            "migration_priority": meta.get("migration_priority", ""),
            "loc": code.count("\n") + 1 if code else 0,
            "n_inputs": len(n.get("inputs", [])),
            "n_outputs": len(n.get("outputs", [])),
            "topo_order": order[n["id"]],
            "db_refs": db_refs[:20],
            # Tareas nativas de EG sin código SAS extraíble. Se exponen en el
            # nivel superior porque el bloque B2/Paso 3 de la Fase 4 las busca
            # aquí por nombre para decidir su destino una por una.
            "native_task": bool(meta.get("native_task", False)),
            "requires_manual_review": bool(meta.get("requires_manual_review", False)),
            # Query Builder de inspección: tiene SQL, pero su salida no la lee
            # nadie. La entrevista lo pregunta con el SQL a la vista.
            "query_preview": bool(meta.get("query_preview", False)),
            "code_source": meta.get("code_source", ""),
            "flags": {
                "has_db": bool(refs),
                "has_macro": "&" in code or "%MACRO" in upper,
                "has_import_file": "PROC IMPORT" in upper,
                "has_export_file": "PROC EXPORT" in upper,
                "native_task": bool(meta.get("native_task", False)),
                "requires_manual_review": bool(meta.get("requires_manual_review", False)),
            },
        })

        for libref, table in refs:
            entry = libref_tables[libref].setdefault(table, {"access": "read", "node_ids": set()})
            entry["node_ids"].add(n["id"])
            if (libref, table) in writes:
                entry["access"] = "both" if entry["access"] == "read" and len(entry["node_ids"]) > 1 else "write"
        for libref, table in writes:
            if libref in libref_tables and table in libref_tables[libref]:
                acc = libref_tables[libref][table]["access"]
                libref_tables[libref][table]["access"] = "write" if acc == "write" else "both"

        for m in re.finditer(r"\bCONNECT\s+TO\s+(\w+)", upper):
            connect_to.append({"node_id": n["id"], "engine": m.group(1)})

    nodes_index = {
        "generated_at": now,
        "total_nodes": len(nodes),
        "nodes": sorted(index_nodes, key=lambda x: x["topo_order"]),
    }
    dump_json(state / "nodes_index.json", nodes_index, indent=None, separators=(",", ":"))

    # ── db_evidence.json ────────────────────────────────────────
    librefs_out = []
    unverified = []
    for libref, tables in sorted(libref_tables.items()):
        all_nodes = sorted({nid for t in tables.values() for nid in t["node_ids"]})
        if libref in declared_db:
            source = "libname_statement"
        elif libref in summary_libs:
            source = "flow_summary_library"
        else:
            source = "qualified_reference"
        entry = {
            "libref": libref,
            "source": source,
            "engine_hint": declared_db.get(libref, ""),
            "node_count": len(all_nodes),
            "table_count": len(tables),
            "tables": [
                {"table": t, "access": info["access"], "node_ids": sorted(info["node_ids"])[:20]}
                for t, info in sorted(tables.items())
            ],
        }
        # Sin declaración (LIBNAME/flow_summary) y con poca evidencia → confirmar en B4b
        declared = libref in declared_db or libref in summary_libs
        if not declared and len(all_nodes) <= 2:
            unverified.append({"prefix": libref, "node_ids": all_nodes,
                               "tables": sorted(tables)})
        else:
            librefs_out.append(entry)

    db_evidence = {
        "generated_at": now,
        "librefs": librefs_out,
        "unverified_prefixes": unverified,
        "connect_to_statements": connect_to,
        "detection_notes": [
            "librefs detectados desde el código (LIBNAME + referencias calificadas), no desde metadata de nodos",
            "se excluyen WORK, aliases SQL declarados en FROM/JOIN, formatos numéricos y extensiones de archivo",
            "unverified_prefixes: prefijos sin LIBNAME y con poca evidencia — confirmar con el usuario en el bloque B4b",
        ],
    }
    dump_json(state / "db_evidence.json", db_evidence, indent=1)

    # ── http_evidence.json ──────────────────────────────────────
    http_evidence = build_http_evidence(nodes, generated_at=now)
    dump_json(state / "http_evidence.json", http_evidence, indent=1)

    # Una línea, a stderr: el tamaño en KB de cada índice medía el trabajo del
    # parser, no una decisión que el usuario pueda tomar. Lo accionable es
    # cuántos librefs quedan SIN confirmar (los que la fase 4 va a preguntar).
    import sys

    print(
        f"    {len(librefs_out)} librefs ({len(unverified)} por confirmar) · "
        f"{len(http_evidence['hosts'])} hosts HTTP "
        f"({len(http_evidence['nodes_with_dynamic_url'])} con URL dinámica)",
        file=sys.stderr, flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Construye nodes_index.json y db_evidence.json")
    parser.add_argument("--state-dir", default="state/", help="Directorio de estado")
    args = parser.parse_args()
    try:
        build(args.state_dir)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
