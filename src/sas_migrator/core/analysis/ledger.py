#!/usr/bin/env python3
"""Ledger de análisis nodo por nodo — state/analysis_progress.json.

Saca la garantía de cobertura del contexto del LLM (que se llena y olvida) y la
pone en disco. El orquestador invoca a code-analyst por Process Flow; el agente
marca cada nodo revisado con una nota de 1 línea; el gate 2 exige cero pending.

Usage:
    python analysis_ledger.py init   [--state-dir state]
    python analysis_ledger.py mark   --nodes id1,id2 [--note "..."] [--state-dir state]
    python analysis_ledger.py sync   [--state-dir state]
    python analysis_ledger.py status [--state-dir state]

`init` es idempotente: re-ejecutarlo preserva los nodos ya marcados `reviewed`.
`mark` sale con código 1 si algún id no existe en el ledger.
`sync` incorpora al ledger los archivos de revisión que escribe code-analyst en
state/analysis_reviews/<pfd_id>.json (el agente no tiene `execute`, por eso
escribe archivos y este subcomando —corrido por el orquestador— los consolida).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

LEDGER_FILE = "analysis_progress.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_ledger(state_dir: Path) -> dict:
    path = state_dir / LEDGER_FILE
    if not path.exists():
        print(f"ERROR: {path} no existe — correr primero: analysis_ledger.py init", file=sys.stderr)
        raise SystemExit(1)
    return json.loads(path.read_text(encoding="utf-8"))


def save_ledger(state_dir: Path, ledger: dict) -> None:
    nodes = ledger.get("nodes", [])
    ledger["total_nodes"] = len(nodes)
    ledger["reviewed"] = sum(1 for n in nodes if n.get("status") == "reviewed")
    ledger["pending"] = ledger["total_nodes"] - ledger["reviewed"]
    (state_dir / LEDGER_FILE).write_text(
        json.dumps(ledger, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def cmd_init(state_dir: Path) -> int:
    index_path = state_dir / "nodes_index.json"
    if not index_path.exists():
        print("ERROR: nodes_index.json no existe — correr primero build_indexes.py", file=sys.stderr)
        return 1
    index = json.loads(index_path.read_text(encoding="utf-8"))

    previous: dict[str, dict] = {}
    ledger_path = state_dir / LEDGER_FILE
    if ledger_path.exists():
        old = json.loads(ledger_path.read_text(encoding="utf-8"))
        previous = {n["node_id"]: n for n in old.get("nodes", [])}

    nodes = []
    for n in index.get("nodes", []):
        node_id = n.get("id", "")
        if not node_id:
            continue
        prior = previous.get(node_id, {})
        nodes.append(
            {
                "node_id": node_id,
                "batch": n.get("pfd_id", "") or "",
                "status": prior.get("status", "pending"),
                "note": prior.get("note", ""),
                "reviewed_at": prior.get("reviewed_at"),
            }
        )

    ledger = {"generated_at": _now(), "nodes": nodes}
    save_ledger(state_dir, ledger)
    print(json.dumps({"total_nodes": ledger["total_nodes"], "reviewed": ledger["reviewed"], "pending": ledger["pending"]}))
    return 0


def cmd_mark(state_dir: Path, node_ids: list[str], note: str) -> int:
    ledger = load_ledger(state_dir)
    by_id = {n["node_id"]: n for n in ledger.get("nodes", [])}

    unknown = [nid for nid in node_ids if nid not in by_id]
    if unknown:
        print(f"ERROR: ids desconocidos en el ledger: {unknown}", file=sys.stderr)
        return 1

    for nid in node_ids:
        entry = by_id[nid]
        entry["status"] = "reviewed"
        entry["reviewed_at"] = _now()
        if note:
            entry["note"] = note

    save_ledger(state_dir, ledger)
    print(json.dumps({"marked": node_ids, "pending": ledger["pending"]}))
    return 0


def cmd_sync(state_dir: Path) -> int:
    ledger = load_ledger(state_dir)
    by_id = {n["node_id"]: n for n in ledger.get("nodes", [])}

    reviews_dir = state_dir / "analysis_reviews"
    synced: list[str] = []
    unknown: list[str] = []
    if reviews_dir.exists():
        for review_file in sorted(reviews_dir.glob("*.json")):
            try:
                doc = json.loads(review_file.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"ERROR: {review_file.name} ilegible: {exc}", file=sys.stderr)
                return 1
            for review in doc.get("reviews", []):
                node_id = str(review.get("node_id", "")).strip()
                if not node_id:
                    continue
                if node_id not in by_id:
                    unknown.append(f"{review_file.name}:{node_id}")
                    continue
                entry = by_id[node_id]
                if entry["status"] != "reviewed":
                    entry["status"] = "reviewed"
                    entry["reviewed_at"] = _now()
                    synced.append(node_id)
                note = str(review.get("note", "")).strip()
                if note:
                    entry["note"] = note

    save_ledger(state_dir, ledger)
    print(json.dumps({"synced": len(synced), "unknown": unknown, "pending": ledger["pending"]}, ensure_ascii=False))
    return 1 if unknown else 0


def cmd_status(state_dir: Path) -> int:
    ledger = load_ledger(state_dir)
    pending_by_batch: dict[str, list[str]] = {}
    for n in ledger.get("nodes", []):
        if n.get("status") != "reviewed":
            pending_by_batch.setdefault(n.get("batch", "") or "(sin flujo)", []).append(n["node_id"])
    print(
        json.dumps(
            {
                "total_nodes": ledger.get("total_nodes", 0),
                "reviewed": ledger.get("reviewed", 0),
                "pending": ledger.get("pending", 0),
                "pending_by_batch": pending_by_batch,
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Ledger de análisis nodo por nodo")
    parser.add_argument("command", choices=["init", "mark", "sync", "status"])
    parser.add_argument("--state-dir", default="state")
    parser.add_argument("--nodes", default="", help="ids separados por coma (para mark)")
    parser.add_argument("--note", default="", help="nota de 1 línea (para mark)")
    args = parser.parse_args()

    state_dir = Path(args.state_dir)
    if args.command == "init":
        return cmd_init(state_dir)
    if args.command == "mark":
        node_ids = [x.strip() for x in args.nodes.split(",") if x.strip()]
        if not node_ids:
            print("ERROR: mark requiere --nodes id1,id2", file=sys.stderr)
            return 1
        return cmd_mark(state_dir, node_ids, args.note)
    if args.command == "sync":
        return cmd_sync(state_dir)
    return cmd_status(state_dir)


if __name__ == "__main__":
    raise SystemExit(main())
