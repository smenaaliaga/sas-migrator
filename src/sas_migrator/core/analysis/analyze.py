#!/usr/bin/env python3
"""
Fase 2: Genera los artefactos de análisis SAS desde los nodos extraídos.

Produce (determinista, sin texto redaccional):
- state/code_smells.json        — smells por reglas genéricas
- state/lineage.json            — linaje dataset-a-dataset desde el DAG
- state/analysis_evidence.json  — evidencia cuantificada para que el
  code-analyst redacte las fichas M-xxx (duplicación, macro vars,
  dependencias de BD, inventario de PROCs, nodos complejos, hits de
  seguridad, rutas/fechas hardcodeadas)
Y actualiza metadata (classification/complexity/priority) en state/nodes/*.json.

Este script NO escribe improvements_proposed.yaml: las fichas M-xxx las
redacta el agente code-analyst a partir de analysis_evidence.json, citando
la evidencia real del proyecto.

Uso: python .github/skills/sas-code-analysis/scripts/generate_analysis.py
     (ejecutar desde la raíz del workspace)
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from sas_migrator.core.parser.statements import DEFAULT_DB_ENGINES, resolve_db_engines
from sas_migrator.core.utils.fsio import dump_json
from sas_migrator.core.utils.node_io import decode_node_code, dump_node

# Consolas Windows cp1252 no soportan "✓" — forzar UTF-8 en stdout.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Default histórico; main() acepta state_dir explícito (fix del hardcode v1).
DEFAULT_STATE = Path("state")
NOW = datetime.now(UTC).isoformat()

FILE_EXTENSIONS = {"XLSX", "XLS", "CSV", "TXT", "PDF", "HTML", "SAS7BDAT"}


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def load_nodes(nodes_dir: Path) -> dict[str, dict]:
    nodes = {}
    for p in sorted(nodes_dir.glob("*.json")):
        n = decode_node_code(json.loads(p.read_text(encoding="utf-8")))
        nodes[n["id"]] = n
    return nodes


def sink_node_ids(nodes: dict, flow_graph: dict) -> set[str]:
    """Nodos sumidero del DAG: nadie consume sus salidas."""
    with_outgoing = {e.get("source") for e in flow_graph.get("edges", []) if e.get("source")}
    return {nid for nid in nodes if nid not in with_outgoing}


def sql_aliases_in_code(code: str) -> set[str]:
    """Aliases declarados en FROM/JOIN (ej: `FROM tabla t1`, `JOIN x AS t2`)."""
    aliases = set()
    for m in re.finditer(
        r"\b(?:FROM|JOIN)\s+[\w.&]+\s+(?:AS\s+)?([A-Z][A-Z0-9_]{0,7})\b",
        code.upper(),
    ):
        alias = m.group(1)
        if alias not in ("ON", "WHERE", "GROUP", "ORDER", "LEFT", "RIGHT",
                         "INNER", "OUTER", "FULL", "JOIN", "AS", "USING"):
            aliases.add(alias)
    return aliases


def db_librefs_in_code(code: str) -> set[str]:
    """Referencias `LIBREF.TABLA` en el código, excluyendo WORK, extensiones de
    archivo, formatos SAS (`COMMAX12.2`) y aliases SQL (`T1.CAMPO`)."""
    upper = code.upper()
    aliases = sql_aliases_in_code(upper)
    refs = set()
    for m in re.finditer(r"\b([A-Z][A-Z0-9_]{1,7})\.([A-Z0-9_]+)\b", upper):
        libref, member = m.group(1), m.group(2)
        if libref == "WORK" or libref in aliases:
            continue
        if member in FILE_EXTENSIONS or libref in FILE_EXTENSIONS:
            continue
        if member.isdigit():  # formato SAS tipo COMMAX12.2 u 8.2
            continue
        refs.add(f"{libref}.{member}")
    return refs


def declared_db_engines(nodes: dict, db_engines: frozenset[str] | None = None) -> dict[str, str]:
    """Librefs con un LIBNAME de engine de BD → engine.

    Debe coincidir con `declared_libnames` de build_indexes.py: es la misma
    pregunta ("¿este libref es realmente una base de datos?") y dos respuestas
    distintas dejarían a code-analyst e interviewer con inventarios divergentes.
    Ambos usan el mismo set (parser.statements + config del proyecto).
    """
    engines = db_engines if db_engines is not None else DEFAULT_DB_ENGINES
    declared: dict[str, str] = {}
    for node in nodes.values():
        upper = node.get("code", "").upper()
        for m in re.finditer(r"\bLIBNAME\s+([A-Z][A-Z0-9_]{0,7})\s+(\w+)?", upper):
            libref, engine = m.group(1), (m.group(2) or "")
            if engine in engines:
                declared[libref] = engine
    return declared


def classify_node(node: dict, sinks: set[str]) -> dict:
    """Infer classification, complexity, priority from node type/code/DAG position.

    Sin heurísticas de proyecto: la prioridad se deriva de la posición en el
    DAG (nodos sumidero = salidas finales) y de dependencias de BD, no de
    nombres de labels específicos.
    """
    ntype = node.get("node_type", "OTHER")
    label = node.get("label", "").upper()
    code = node.get("code", "")
    outputs = node.get("outputs", [])
    inputs = node.get("inputs", [])

    # Classification
    if ntype == "IMPORT" or "PROC IMPORT" in code.upper():
        cls = "extract"
    elif ntype == "EXPORT" or "PROC EXPORT" in code.upper():
        cls = "load"
    elif ntype == "MACRO" or "%MACRO" in code.upper():
        cls = "macro"
    elif ntype == "QUERY":
        cls = "transform"
    elif any(w in label for w in ["CHECK", "VERIF", "VALID", "TEST"]):
        cls = "validate"
    elif any(w in label for w in ["SALIDA", "OUTPUT", "CARGA", "LOAD", "WRITE"]):
        cls = "load"
    elif ntype in ("DATA_STEP", "PROC_SQL", "PROC_SORT", "PROC_MEANS",
                   "PROC_FREQ", "PROC_TRANSPOSE"):
        cls = "transform"
    else:
        cls = "utility"

    # Complexity
    n_in = len(inputs)
    n_out = len(outputs)
    code_len = len(code)
    has_macro_vars = bool(re.search(r'&\w+', code))
    n_joins = len(re.findall(r'\bJOIN\b', code, re.I))
    n_case = len(re.findall(r'\bCASE\b', code, re.I))

    score = 0
    if n_in > 5:   score += 2
    elif n_in > 2: score += 1
    if n_out > 3:  score += 1
    if code_len > 3000: score += 2
    elif code_len > 1000: score += 1
    if n_joins > 3: score += 2
    elif n_joins > 1: score += 1
    if n_case > 3:  score += 1
    if has_macro_vars: score += 1

    complexity = "high" if score >= 5 else ("medium" if score >= 2 else "low")

    # Migration priority — derivada del DAG y de dependencias de BD. Con la
    # vista v2 todo input viene calificado (WORK explícito), así que "tiene
    # punto" ya no significa "no-WORK": hay que excluir WORK. del criterio.
    has_db_dep = bool(db_librefs_in_code(code)) or any(
        "." in x and not x.upper().startswith("WORK.") for x in inputs
    )
    writes_db = any("." in x and not x.upper().startswith("WORK.") for x in outputs) or bool(
        re.search(r"\b(CREATE\s+TABLE|INSERT\s+INTO|APPEND\s+BASE=)\s*[A-Z][A-Z0-9_]*\.", code, re.I)
    )
    is_final_output = node["id"] in sinks or writes_db or cls == "load"
    is_setup = "AUTOEXEC" in label or "AUTOEXEC" in node.get("pfd_label", "").upper()

    if is_setup or (is_final_output and complexity == "high"):
        priority = "critical"
    elif is_final_output or (complexity == "high" and has_db_dep):
        priority = "high"
    elif complexity == "medium" or has_db_dep:
        priority = "medium"
    else:
        priority = "low"

    return {"classification": cls, "complexity": complexity,
            "migration_priority": priority}


# ─────────────────────────────────────────────────────────────
# Code smells detection (reglas genéricas, sin valores de proyecto)
# ─────────────────────────────────────────────────────────────
_smell_counter = 0

SMELL_RULES = [
    ("hardcoded_path", "error", "maintainability",
     r"[A-Z]:[\\\/][^\s\"']+|\/sasdata\/[^\s\"']+|\\\\[^\s\"']+",
     "Ruta de archivo hardcodeada"),
    ("date_hardcoded", "warning", "reproducibility",
     r"\b(19|20)\d{2}[01]\d[0-3]\d\b|'[0-9]{4}-[0-9]{2}-[0-9]{2}'",
     "Fecha hardcodeada en código"),
    ("proc_sql_cartesian", "error", "performance",
     r"FROM\s+\w+\s*,\s*\w+(?!\s+WHERE)",
     "Posible producto cartesiano (JOIN implícito sin WHERE)"),
    ("credential_leak", "error", "security",
     r'(?i)(password|passwd|pwd)\s*=\s*["\']?\S+',
     "Posible credencial expuesta en código"),
    ("select_star", "info", "quality",
     r"SELECT\s+\*",
     "SELECT * — columnas implícitas dificultan mantenimiento"),
    ("implicit_missing", "warning", "quality",
     r"\bIF\b[^;]+\bTHEN\s+\w+\s*=[^;]+;\s*(?!/\*)",
     "IF sin ELSE puede producir missing values implícitos en DATA step"),
    ("no_proc_quit", "info", "quality",
     r"PROC\s+SQL\b(?!.*QUIT)",
     "PROC SQL sin QUIT explícito"),
]


def find_repeated_magic_numbers(nodes: dict, min_nodes: int = 3) -> dict[str, list[str]]:
    """Números literales ≥3 dígitos que se repiten en varios nodos (constantes candidatas)."""
    number_nodes: dict[str, set[str]] = defaultdict(set)
    for nid, node in nodes.items():
        for m in re.finditer(r"(?<![\w.])(\d{3,})(?![\w.])", node.get("code", "")):
            val = m.group(1)
            if val.startswith(("19", "20")) and len(val) in (4, 8):
                continue  # probablemente años/fechas, cubiertos por date_hardcoded
            number_nodes[val].add(nid)
    return {num: sorted(nids) for num, nids in number_nodes.items() if len(nids) >= min_nodes}


def detect_smells(node: dict, magic_numbers: dict[str, list[str]]) -> list[dict]:
    global _smell_counter
    code = node.get("code", "")
    nid = node["id"]
    label = node.get("label", "")
    smells = []
    for stype, severity, category, pattern, desc in SMELL_RULES:
        for m in re.finditer(pattern, code, re.I | re.S):
            fragment = code[max(0, m.start()-30):m.end()+30].strip().replace("\n", " ")[:120]
            _smell_counter += 1
            smells.append({
                "id": f"CS-{_smell_counter:04d}",
                "node_id": nid,
                "category": category,
                "description": f"[{label}] {desc}",
                "severity": severity,
                "line_hint": fragment,
                "recommendation": f"Refactorizar patrón '{stype}' en nodo {nid}.",
            })
            break  # one smell per rule per node

    # Números mágicos: solo los que se repiten en ≥3 nodos del proyecto
    node_magic = [num for num, nids in magic_numbers.items() if nid in nids]
    if node_magic:
        _smell_counter += 1
        smells.append({
            "id": f"CS-{_smell_counter:04d}",
            "node_id": nid,
            "category": "maintainability",
            "description": f"[{label}] Números mágicos repetidos en varios nodos: {sorted(node_magic)[:5]}",
            "severity": "warning",
            "line_hint": str(sorted(node_magic)[0]),
            "recommendation": "Extraer como constantes nombradas en la celda de configuración del notebook.",
        })

    # Variables macro dinámicas
    macro_vars = re.findall(r'&(\w+)', code)
    if macro_vars:
        unique_mv = sorted(set(macro_vars))[:5]
        _smell_counter += 1
        smells.append({
            "id": f"CS-{_smell_counter:04d}",
            "node_id": nid,
            "category": "reproducibility",
            "description": f"[{label}] Variables macro dinámicas: {unique_mv}.",
            "severity": "warning",
            "line_hint": f"&{unique_mv[0]}",
            "recommendation": "Declarar como parámetros/constantes en la celda de configuración del notebook (Papermill-compatible).",
        })
    return smells


# ─────────────────────────────────────────────────────────────
# Linaje básico desde DAG
# ─────────────────────────────────────────────────────────────
def build_lineage(nodes: dict, flow_graph: dict) -> list[dict]:
    """Build dataset-level lineage entries from DAG edges.

    Los inputs/outputs de los nodos vienen del parser v2 (``LIB.TABLA``
    calificados), así que el match primario es por nombre calificado. El match
    por nombre corto queda como fallback para pares donde los librefs difieren
    (misma tabla vista con prefijos distintos) — señal débil, pero mejor que
    perder la arista de linaje.
    """
    entries = []
    seen = set()
    for edge in flow_graph.get("edges", []):
        src_id = edge.get("source")
        tgt_id = edge.get("target")
        if not src_id or not tgt_id:
            continue
        src = nodes.get(src_id, {})
        tgt = nodes.get(tgt_id, {})
        src_out = src.get("outputs", [])
        tgt_in = tgt.get("inputs", [])

        # 1) match calificado LIB.TABLA
        src_q = {ds.upper(): ds for ds in src_out}
        tgt_q = {ds.upper(): ds for ds in tgt_in}
        shared_q = set(src_q) & set(tgt_q)
        pairs = [(src_q[q], tgt_q[q]) for q in shared_q]

        # 2) fallback por nombre corto, solo sobre lo no matcheado en (1)
        def _short(ds: str) -> str:
            return ds.upper().split(".")[-1]

        src_s = {_short(ds): ds for q, ds in src_q.items() if q not in shared_q}
        tgt_s = {_short(ds): ds for q, ds in tgt_q.items() if q not in shared_q}
        pairs.extend((src_s[s], tgt_s[s]) for s in set(src_s) & set(tgt_s))

        if pairs:
            tgt_type = tgt.get("node_type", "")
            if tgt_type == "PROC_SORT":                   ttype = "reshape"
            elif tgt_type in ("PROC_MEANS", "PROC_FREQ"): ttype = "aggregate"
            elif tgt_type == "PROC_TRANSPOSE":            ttype = "reshape"
            elif "JOIN" in tgt.get("code", "").upper():   ttype = "join"
            elif tgt_type == "DATA_STEP":                 ttype = "derive"
            else:                                          ttype = "pass_through"

            for source_ds, target_ds in sorted(pairs):
                key = (tgt_id, source_ds)
                if key not in seen:
                    seen.add(key)
                    entries.append({
                        "node_id": tgt_id,
                        "source_dataset": source_ds,
                        "source_column": "*",
                        "target_dataset": target_ds,
                        "target_column": "*",
                        "transformation": ttype,
                    })
    return entries


# ─────────────────────────────────────────────────────────────
# Evidencia para fichas M-xxx (datos, sin redacción)
# ─────────────────────────────────────────────────────────────
def _normalize_block(text: str) -> str:
    """Normaliza un bloque de código para detectar duplicación."""
    t = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    t = re.sub(r"\s+", " ", t).strip().upper()
    return t


def find_duplicated_blocks(nodes: dict, min_len: int = 60, min_nodes: int = 3) -> list[dict]:
    """Statements normalizados que se repiten literalmente en ≥min_nodes nodos."""
    stmt_nodes: dict[str, set[str]] = defaultdict(set)
    for nid, node in nodes.items():
        for stmt in node.get("code", "").split(";"):
            norm = _normalize_block(stmt)
            if len(norm) >= min_len:
                stmt_nodes[norm].add(nid)
    dups = [
        {"fragment": stmt[:200], "node_count": len(nids), "node_ids": sorted(nids)}
        for stmt, nids in stmt_nodes.items() if len(nids) >= min_nodes
    ]
    return sorted(dups, key=lambda d: -d["node_count"])[:25]


def find_repeated_expressions(nodes: dict, min_nodes: int = 5) -> list[dict]:
    """Expresiones CASE WHEN / cálculos que se repiten en varios nodos."""
    expr_nodes: dict[str, set[str]] = defaultdict(set)
    for nid, node in nodes.items():
        code = node.get("code", "")
        for m in re.finditer(r"CASE\s+WHEN\b.{10,200}?\bEND\b", code, re.I | re.S):
            expr_nodes[_normalize_block(m.group(0))].add(nid)
        for m in re.finditer(r"\b\w+\s*\*\s*\w+\s*/\s*\d+\b", code):
            expr_nodes[_normalize_block(m.group(0))].add(nid)
    reps = [
        {"expression": expr[:160], "node_count": len(nids), "node_ids": sorted(nids)}
        for expr, nids in expr_nodes.items() if len(nids) >= min_nodes
    ]
    return sorted(reps, key=lambda r: -r["node_count"])[:25]


def build_evidence(nodes: dict, all_smells: list[dict],
                   magic_numbers: dict[str, list[str]],
                   db_engines: frozenset[str] | None = None) -> dict:
    # Macro variables por frecuencia
    mv_nodes: dict[str, set[str]] = defaultdict(set)
    for nid, node in nodes.items():
        for mv in set(re.findall(r'&(\w+)', node.get("code", ""))):
            mv_nodes[mv].add(nid)
    macro_variables = sorted(
        ({"variable": mv, "node_count": len(nids), "node_ids": sorted(nids)[:20]}
         for mv, nids in mv_nodes.items()),
        key=lambda m: -m["node_count"],
    )[:20]

    # Dependencias de BD (librefs y tablas). Un `LIBREF.TABLA` en el código NO
    # prueba que el libref sea una base de datos: solo lo es si hay un LIBNAME
    # con engine de BD. El resto son prefijos sin confirmar, y así deben
    # presentarse — db_evidence.json hace la misma distinción, y si aquí se
    # omitiera, el code-analyst vería 21 librefs de BD donde el interviewer ve 10.
    declared_db_librefs = declared_db_engines(nodes, db_engines)
    libref_tables: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for nid, node in nodes.items():
        for ref in db_librefs_in_code(node.get("code", "")):
            libref, table = ref.split(".", 1)
            libref_tables[libref][table].add(nid)
    db_dependencies = [
        {
            "libref": libref,
            "verified_db": libref in declared_db_librefs,
            "engine": declared_db_librefs.get(libref, ""),
            "table_count": len(tables),
            "node_count": len({n for nids in tables.values() for n in nids}),
            "tables": [
                {"table": t, "node_ids": sorted(nids)[:20]}
                for t, nids in sorted(tables.items())
            ],
        }
        for libref, tables in sorted(libref_tables.items())
    ]

    # Inventario de PROCs
    proc_inventory = Counter(node.get("node_type", "OTHER") for node in nodes.values())

    # Nodos más complejos
    top_complex = sorted(
        (
            {
                "node_id": nid,
                "label": node.get("label", ""),
                "loc": node.get("code", "").count("\n") + 1,
                "complexity": node.get("metadata", {}).get("complexity", ""),
                "priority": node.get("metadata", {}).get("migration_priority", ""),
            }
            for nid, node in nodes.items()
        ),
        key=lambda n: -n["loc"],
    )[:15]

    by_type = defaultdict(list)
    for s in all_smells:
        by_type[s["category"]].append(s["id"])

    return {
        "generated_at": NOW,
        "total_nodes": len(nodes),
        "duplicated_code_blocks": find_duplicated_blocks(nodes),
        "repeated_expressions": find_repeated_expressions(nodes),
        "repeated_magic_numbers": [
            {"value": num, "node_count": len(nids), "node_ids": nids[:20]}
            for num, nids in sorted(magic_numbers.items(), key=lambda kv: -len(kv[1]))
        ][:20],
        "macro_variables": macro_variables,
        "db_dependencies": db_dependencies,
        "proc_inventory": dict(sorted(proc_inventory.items())),
        "top_complex_nodes": top_complex,
        "security_hits": [s for s in all_smells if s["category"] == "security"],
        "hardcoded_paths_dates": [
            s for s in all_smells
            if s["category"] in ("maintainability", "reproducibility")
            and ("Ruta" in s["description"] or "Fecha" in s["description"])
        ],
        "smells_by_category": {k: v for k, v in sorted(by_type.items())},
    }


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────
def main(state_dir: Path | None = None):
    # Contador global de CS-xxxx: resetear por corrida — dos ejecuciones en el
    # mismo proceso deben producir IDs idénticos (garantía de determinismo).
    global _smell_counter
    _smell_counter = 0
    state = Path(state_dir) if state_dir is not None else DEFAULT_STATE
    nodes_dir = state / "nodes"
    # project_config.yaml vive en el workspace (padre de state/).
    db_engines = resolve_db_engines(state.resolve().parent)
    print("Cargando flow_graph.json ...")
    flow_graph = json.loads((state / "flow_graph.json").read_text(encoding="utf-8"))

    print("Cargando nodos ...")
    nodes = load_nodes(nodes_dir)
    sinks = sink_node_ids(nodes, flow_graph)

    # ── 1. Classify all nodes and update metadata ──────────────
    print(f"Clasificando {len(nodes)} nodos ...")
    for nid, node in nodes.items():
        if "metadata" not in node:
            node["metadata"] = {}
        meta = node["metadata"]

        if "classification" not in meta:
            meta.update(classify_node(node, sinks))

        dump_node(nodes_dir / f"{nid}.json", node)
    print("  ✓ Nodos clasificados")

    # ── 2. Code smells ─────────────────────────────────────────
    print("Detectando code smells ...")
    magic_numbers = find_repeated_magic_numbers(nodes)
    all_smells = []
    for nid, node in nodes.items():
        all_smells.extend(detect_smells(node, magic_numbers))

    code_smells = {
        "generated_at": NOW,
        "total_smells": len(all_smells),
        "summary": {
            "error":   sum(1 for s in all_smells if s["severity"] == "error"),
            "warning": sum(1 for s in all_smells if s["severity"] == "warning"),
            "info":    sum(1 for s in all_smells if s["severity"] == "info"),
        },
        "smells": all_smells,
    }
    dump_json(state / "code_smells.json", code_smells)
    print(f"  ✓ {len(all_smells)} smells detectados "
          f"(E:{code_smells['summary']['error']} "
          f"W:{code_smells['summary']['warning']} "
          f"I:{code_smells['summary']['info']})")

    # ── 3. Evidencia para fichas M-xxx ─────────────────────────
    print("Construyendo evidencia de análisis ...")
    evidence = build_evidence(nodes, all_smells, magic_numbers, db_engines)
    dump_json(state / "analysis_evidence.json", evidence)
    print(f"  ✓ analysis_evidence.json: "
          f"{len(evidence['duplicated_code_blocks'])} bloques duplicados, "
          f"{len(evidence['repeated_expressions'])} expresiones repetidas, "
          f"{len(evidence['macro_variables'])} macro vars, "
          f"{sum(1 for d in evidence['db_dependencies'] if d['verified_db'])} librefs de BD "
          f"confirmados por LIBNAME "
          f"(+{sum(1 for d in evidence['db_dependencies'] if not d['verified_db'])} prefijos sin confirmar)")

    # ── 4. Lineage ─────────────────────────────────────────────
    print("Construyendo linaje ...")
    entries = build_lineage(nodes, flow_graph)
    # `entry_count` cuenta entradas de linaje, NO Process Flows. El nombre
    # anterior (`flow_count`) hacía que un consumidor leyera 28 donde el
    # proyecto tiene 18 flujos. `covered_nodes` deja explícita la cobertura
    # real, que es parcial por construcción.
    lineage = {
        "generated_at": NOW,
        "node_count": len(nodes),
        "entry_count": len(entries),
        "covered_nodes": len({e["node_id"] for e in entries}),
        "lineage": entries,
    }
    dump_json(state / "lineage.json", lineage)
    print(f"  ✓ {len(lineage['lineage'])} entradas de linaje "
          f"({lineage['covered_nodes']}/{len(nodes)} nodos cubiertos)")

    # ── Summary ────────────────────────────────────────────────
    critical = [n for n, nd in nodes.items()
                if nd.get("metadata", {}).get("migration_priority") == "critical"]
    print("\n" + "=" * 60)
    print("ANÁLISIS COMPLETADO")
    print(f"  Nodos clasificados: {len(nodes)}")
    print(f"  Smells detectados:  {len(all_smells)}")
    print(f"  Nodos críticos:     {len(critical)}")
    print(f"  Entradas de linaje: {len(entries)}")
    print("  Evidencia M-xxx:    state/analysis_evidence.json")
    print("  → El code-analyst redacta improvements_proposed.yaml desde la evidencia.")
    print("=" * 60)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Análisis determinista de nodos SAS")
    parser.add_argument("--state-dir", default=None, help="Directorio de estado (default: state/)")
    args = parser.parse_args()
    main(args.state_dir)
