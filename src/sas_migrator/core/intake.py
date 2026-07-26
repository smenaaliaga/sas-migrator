#!/usr/bin/env python3
"""Phase 0 (Intake): scan the input/ folder and write state/intake.json.

Canonical intake scanner for the SAS migration pipeline. Catalogs the .egp
project, data files, and documentation the user placed under input/, and
persists the result as state/intake.json.

Only input/ is scanned on purpose: files outside input/ belong to the migrator
system (its own README.md, guia-uso.html, etc.), not to the SAS flow being
migrated, so they must never be cataloged as migration inputs.

Usage:
    python .github/skills/migration-state/scripts/intake_scan.py
    python .github/skills/migration-state/scripts/intake_scan.py --input-dir input --state-dir state

Exit codes:
    0 = intake.json written
    1 = no .egp file found under input/egp/ (nothing to migrate)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DATA_SUFFIXES = {".csv", ".xlsx", ".xls", ".parquet"}
DOC_SUFFIXES = {".md", ".docx", ".pdf", ".html", ".txt"}


def scan(input_dir: Path) -> dict:
    intake: dict = {
        "egp_files": [],
        "data_files": [],
        "doc_files": [],
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }

    egp_dir = input_dir / "egp"
    for f in sorted(egp_dir.glob("*.egp")):
        if f.is_file():
            intake["egp_files"].append({"path": str(f), "size_bytes": f.stat().st_size})

    data_dir = input_dir / "data"
    for f in sorted(data_dir.glob("*")):
        if f.is_file() and f.suffix.lower() in DATA_SUFFIXES:
            intake["data_files"].append(
                {"path": str(f), "type": f.suffix.lstrip("."), "size_bytes": f.stat().st_size}
            )

    docs_dir = input_dir / "docs"
    for f in sorted(docs_dir.glob("*")):
        if f.is_file() and f.suffix.lower() in DOC_SUFFIXES:
            intake["doc_files"].append({"path": str(f)})

    return intake


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan input/ and write state/intake.json")
    parser.add_argument("--input-dir", default="input", help="Input folder to scan (default: input)")
    parser.add_argument("--state-dir", default="state", help="State folder to write to (default: state)")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    intake = scan(input_dir)

    out = state_dir / "intake.json"
    out.write_text(json.dumps(intake, indent=2, ensure_ascii=False), encoding="utf-8")

    n_egp = len(intake["egp_files"])
    n_data = len(intake["data_files"])
    n_docs = len(intake["doc_files"])
    print(f"OK intake.json: {n_egp} .egp + {n_data} datos + {n_docs} docs -> {out}")

    if n_egp == 0:
        print("ERROR: no se encontró ningún .egp en input/egp/. Agrega el archivo y vuelve a correr.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
