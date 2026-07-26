#!/usr/bin/env python3
"""Checkpoint determinista de la Fase 6 — qué nodos del plan ya están traducidos.

Compara `translation_plan.targets` contra `sas_python_mapping.json` y la
existencia/validez de los notebooks referenciados. Permite reanudar la
generación pasando al translator solo los nodos pendientes, sin releer nada
con el LLM.

Usage:
    python generation_status.py [--state-dir state] [--output-dir output]

Exit code 0 siempre que pueda calcular el estado (pendientes > 0 no es error).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build_status(state_dir: Path, output_dir: Path) -> dict:
    plan_path = state_dir / "translation_plan.json"
    if not plan_path.exists():
        raise FileNotFoundError(f"{plan_path} no existe — la Fase 5 debe correr primero")
    plan = _load(plan_path)

    mapping_path = state_dir / "sas_python_mapping.json"
    mapped_ids: set[str] = set()
    if mapping_path.exists():
        mapping_doc = _load(mapping_path)
        mappings = mapping_doc.get(
            "mappings", mapping_doc if isinstance(mapping_doc, list) else []
        )
        mapped_ids = {
            str(m.get("node_id", "")) for m in mappings if isinstance(m, dict) and m.get("node_id")
        }

    ignored = {str(x) for x in plan.get("ignored_nodes", [])}

    done: list[str] = []
    pending: list[str] = []
    by_notebook: dict[str, dict] = {}
    for target in plan.get("targets", []):
        node_id = str(target.get("node_id", ""))
        if not node_id or node_id in ignored:
            continue
        nb_rel = str(target.get("notebook_path", "")) or "(sin notebook)"
        entry = by_notebook.setdefault(
            nb_rel,
            {"done": [], "pending": [], "notebook_exists": None},
        )
        if entry["notebook_exists"] is None and nb_rel != "(sin notebook)":
            # notebook_path es relativo a la raíz del workspace (state_dir.parent),
            # no al cwd del proceso.
            nb_path = Path(state_dir).parent / nb_rel
            exists = nb_path.exists()
            if exists:
                try:
                    _load(nb_path)
                except Exception:
                    exists = False  # notebook corrupto = no cuenta como generado
            entry["notebook_exists"] = exists

        if node_id in mapped_ids and entry["notebook_exists"]:
            done.append(node_id)
            entry["done"].append(node_id)
        else:
            pending.append(node_id)
            entry["pending"].append(node_id)

    return {
        "total_targets": len(done) + len(pending),
        "done_count": len(done),
        "pending_count": len(pending),
        "done": done,
        "pending": pending,
        "by_notebook": by_notebook,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Estado de generación por nodo (Fase 6)")
    parser.add_argument("--state-dir", default="state")
    parser.add_argument("--output-dir", default="output")
    args = parser.parse_args()

    try:
        status = build_status(Path(args.state_dir), Path(args.output_dir))
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(status, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
