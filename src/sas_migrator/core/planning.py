#!/usr/bin/env python3
"""Assemble state/translation_plan.json deterministically from Phase 2-4 state.

The translation plan is ~90% a mechanical join over artifacts that already
reference nodes by id. Building it by hand (LLM) is slow and non-deterministic:
node ordering, notebook grouping, and improvement/preprocess assignment can
drift between runs. This script produces the full plan skeleton so the agent
only reviews strategy/notes and presents it to the user for approval (the
Phase 5 approval step is unchanged; user_approved stays false here).

Granularity: one TranslationTarget per non-ignored node (matching the Pydantic
model), grouped into notebooks by notebook_path. Notebooks are numbered per
in-scope Process Flow in flow_summary order: NB-NN_<notebook_slug>.ipynb.

Inputs (from --state-dir):
    required : flow_summary.json, nodes_index.json, migration_state.json
    optional : ignored_nodes.yaml, approved_improvements.yaml,
               preprocess_playbook.yaml, file_mapping.json, flow_graph.json
Loaders are tolerant of container shape (bare list / {key: list} / {id: obj}).

Usage:
    python .github/skills/sas-to-python/scripts/build_translation_plan.py --state-dir state/

Exit codes:
    0 = translation_plan.json written
    1 = a required input is missing
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

try:
    import yaml
except ImportError:  # yaml is a project dep; degrade gracefully for JSON-only inputs
    yaml = None


# ---------------------------------------------------------------------------
# Tolerant loading
# ---------------------------------------------------------------------------

def load_artifact(path: Path):
    """Load a JSON or YAML artifact, or None if it does not exist."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        if yaml is None:
            return None
        return yaml.safe_load(text)
    return json.loads(text)


def as_list(obj, *keys) -> list:
    """Extract a list from an artifact regardless of its container shape."""
    if obj is None:
        return []
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in keys:
            if isinstance(obj.get(k), list):
                return obj[k]
        vals = list(obj.values())
        if vals and all(isinstance(v, dict) for v in vals):
            return vals
    return []


def to_node_id(item) -> str:
    """Normalize a node reference (string id or dict) to a node id string."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for k in ("node_id", "id", "nodeId"):
            if item.get(k):
                return str(item[k])
    return ""


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def choose_strategy(node_type: str) -> str:
    # Todo se traduce a pandas (incluido PROC SQL). No se usa ningún motor SQL embebido.
    return "pandas"


def build(state: Path) -> dict:
    migration_state = load_artifact(state / "migration_state.json")
    flow_summary = load_artifact(state / "flow_summary.json")
    nodes_index = load_artifact(state / "nodes_index.json")

    missing = [name for name, val in (
        ("migration_state.json", migration_state),
        ("flow_summary.json", flow_summary),
        ("nodes_index.json", nodes_index),
    ) if val is None]
    if missing:
        raise FileNotFoundError(", ".join(missing))

    output_strategy = (migration_state or {}).get("output_strategy", "notebook-flow")

    # node_id -> index metadata
    idx = {n["id"]: n for n in as_list(nodes_index, "nodes")}

    # ignored nodes
    ignored = {to_node_id(x) for x in as_list(
        load_artifact(state / "ignored_nodes.yaml"),
        "ignored_nodes", "ignored", "nodes", "node_ids")}
    ignored.discard("")

    # improvements: node_id -> [M-xxx]; global_improvements for node-agnostic ones
    node_improvements: dict[str, list[str]] = {}
    global_improvements: list[str] = []
    for imp in as_list(load_artifact(state / "approved_improvements.yaml"),
                        "approved_improvements", "improvements", "approved"):
        if not isinstance(imp, dict):
            continue
        status = str(imp.get("status", "approved")).lower()
        if status not in ("approved", ""):
            continue
        imp_id = imp.get("id", "")
        affected = [to_node_id(x) for x in imp.get("affected_nodes", [])]
        affected = [a for a in affected if a]
        if not affected:
            global_improvements.append(imp_id)
        for nid in affected:
            node_improvements.setdefault(nid, []).append(imp_id)

    # file_mapping: node_id -> input files / reference csv (by role)
    node_inputs: dict[str, list[str]] = {}
    node_reference: dict[str, str] = {}
    filepath_to_node: dict[str, str] = {}
    for fm in as_list(load_artifact(state / "file_mapping.json"),
                      "file_mapping", "mappings", "files"):
        if not isinstance(fm, dict):
            continue
        nid = to_node_id(fm)
        fpath = fm.get("file_path", "")
        role = str(fm.get("role", "")).lower()
        if nid and fpath:
            filepath_to_node[fpath] = nid
            if role in ("input", "intermediate"):
                node_inputs.setdefault(nid, []).append(fpath)
            elif role == "output" and nid not in node_reference:
                node_reference[nid] = fpath

    # preprocess_playbook: step -> node (via its file_path -> file_mapping)
    node_preprocess: dict[str, list[str]] = {}
    for step in as_list(load_artifact(state / "preprocess_playbook.yaml"),
                        "preprocess_playbook", "steps", "playbook", "preprocess"):
        if not isinstance(step, dict):
            continue
        step_id = step.get("id", "")
        # A preprocess step has its OWN id (PP-xxx), so resolve its node via an
        # explicit node key or (the common case) its file_path through file_mapping.
        nid = ""
        for k in ("node_id", "node", "nodeId"):
            if step.get(k):
                nid = str(step[k])
                break
        if not nid:
            nid = filepath_to_node.get(step.get("file_path", ""), "")
        if nid and step_id:
            node_preprocess.setdefault(nid, []).append(step_id)

    # dependencies from the DAG edges (predecessors), non-ignored only
    predecessors: dict[str, list[str]] = {}
    flow_graph = load_artifact(state / "flow_graph.json")
    for e in as_list(flow_graph, "edges") if isinstance(flow_graph, dict) else []:
        s, t = e.get("source"), e.get("target")
        if s and t:
            predecessors.setdefault(t, []).append(s)

    # ── assemble targets, grouped by in-scope flow ──────────────────────────
    flows = as_list(flow_summary, "flows")
    targets: list[dict] = []
    notebooks: list[str] = []
    nb_index = 0

    for flow in flows:
        if not flow.get("migratable_candidate", True):
            continue
        flow_nodes = [nid for nid in flow.get("node_ids", []) if nid not in ignored]
        if not flow_nodes:
            continue

        nb_index += 1
        slug = flow.get("notebook_slug") or flow.get("pfd_id") or f"flow{nb_index}"
        if output_strategy == "single":
            notebook_path = "flow.ipynb"
        else:
            notebook_path = f"NB-{nb_index:02d}_{slug}.ipynb"
        if notebook_path not in notebooks:
            notebooks.append(notebook_path)

        # order this flow's nodes by their global topo_order
        flow_nodes.sort(key=lambda nid: idx.get(nid, {}).get("topo_order", 1_000_000))

        for nid in flow_nodes:
            meta = idx.get(nid, {})
            node_type = meta.get("node_type", "")
            deps = sorted({d for d in predecessors.get(nid, []) if d not in ignored})
            targets.append({
                "node_id": nid,
                "node_label": meta.get("label", ""),
                "node_type": node_type,
                "strategy": choose_strategy(node_type),
                "notebook_path": notebook_path,
                "input_datasets": [],
                "output_datasets": [],
                "input_dir": None,
                "input_files": node_inputs.get(nid, []),
                "output_files": [],
                "output_tables": [],
                "reference_csv": node_reference.get(nid),
                "approved_improvements": node_improvements.get(nid, []),
                "preprocess_steps": node_preprocess.get(nid, []),
                "dependencies": deps,
                "notes": "",
            })

    assumptions = [
        "Plan generado por build_translation_plan.py; el agente debe revisar strategy/notes y el usuario aprobar.",
        "strategy = pandas para todos los nodos (incluido PROC SQL); no se usa ningún motor SQL embebido.",
        "input/output_datasets y output_tables se resuelven desde los node files en la Fase 6 (traducción).",
    ]
    if flow_graph is None:
        assumptions.append("flow_graph.json ausente: 'dependencies' quedó vacío (correr build_indexes/extractor).")

    return {
        "project_name": (migration_state or {}).get("project_name", ""),
        "egp_file": (migration_state or {}).get("egp_file", ""),
        "output_strategy": output_strategy,
        "generated_at": datetime.now(UTC).isoformat(),
        "targets": targets,
        "ignored_nodes": sorted(ignored),
        "global_improvements": global_improvements,
        "assumptions": assumptions,
        "user_approved": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build translation_plan.json from state")
    parser.add_argument("--state-dir", default="state", help="State directory (default: state)")
    args = parser.parse_args()

    state = Path(args.state_dir)
    try:
        plan = build(state)
    except FileNotFoundError as e:
        print(f"ERROR: faltan insumos requeridos: {e}", file=sys.stderr)
        return 1

    out = state / "translation_plan.json"
    out.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

    n_nb = len({t["notebook_path"] for t in plan["targets"]})
    n_imp = sum(len(t["approved_improvements"]) for t in plan["targets"]) + len(plan["global_improvements"])
    print(f"OK translation_plan.json: {len(plan['targets'])} targets en {n_nb} notebooks, "
          f"{len(plan['ignored_nodes'])} nodos ignorados, {n_imp} mejoras asignadas -> {out}")
    print("  Revisa strategy/notes y presenta el plan al usuario para aprobación (user_approved=false).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
