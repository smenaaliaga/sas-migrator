#!/usr/bin/env python3
"""Genera state/flow_summary.json a partir de un state/flow_graph.json existente.

Útil para (re)construir el inventario de Process Flows SIN volver a extraer el
.egp, preservando así las clasificaciones que code-analyst escribió en
state/nodes/*.json.

El extractor (egp_extractor.py) ya produce flow_summary.json automáticamente; este
script es para regenerarlo cuando el flow_graph.json ya existe.

Usage:
    python summarize_flows.py [--state-dir state/]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sas_migrator.core.extractors.egp import build_flow_summary
from sas_migrator.core.models.graph import FlowGraph


def main() -> None:
    parser = argparse.ArgumentParser(description="Build flow_summary.json from flow_graph.json")
    parser.add_argument("--state-dir", default="state", help="State directory path")
    args = parser.parse_args()

    state_dir = Path(args.state_dir)
    graph_path = state_dir / "flow_graph.json"
    if not graph_path.exists():
        print(f"ERROR: no existe {graph_path}. Ejecuta primero egp_extractor.py.", file=sys.stderr)
        sys.exit(1)

    graph = FlowGraph.model_validate_json(graph_path.read_text(encoding="utf-8"))
    summary = build_flow_summary(graph)

    out_path = state_dir / "flow_summary.json"
    out_path.write_text(
        json.dumps(summary.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "flow_summary": str(out_path),
                "total_flows": summary.total_flows,
                "migratable_flows": summary.migratable_flows,
                "total_nodes": summary.total_nodes,
                "flows": [
                    {"pfd_label": f.pfd_label, "node_count": f.node_count,
                     "migratable_candidate": f.migratable_candidate}
                    for f in summary.flows
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
