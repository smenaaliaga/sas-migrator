#!/usr/bin/env python3
"""Discover SAS reference outputs and stage them for validation.

Validation compares each SQL Server target table against a SAS reference CSV.
Matching a reference file to a target table is deterministic: the reference
whose filename stem (uppercased) equals the bare target table name is its
ground truth. run_validation.py matches by ``f.stem.upper()`` against the plan's
``output_tables`` normalized as ``LIB.TABLE -> TABLE`` (uppercased), so this
script uses the exact same rule and stages matches under --reference-dir with
the table name as the stem.

This replaces the previous manual, per-run matching-and-copying step.

Usage:
    python .github/skills/validation-protocol/scripts/discover_references.py \\
        --state-dir state/ --input-dir input/data --reference-dir state/reference_outputs/

Exit codes:
    0 = ran successfully (report how many references were staged; zero is valid —
        it just means validation will run in a no-reference mode)
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REFERENCE_SUFFIXES = {".csv", ".txt", ".tsv", ".xlsx", ".xls"}


def load_target_tables(state_dir: Path) -> list[str]:
    """Bare, uppercased target table names from the translation plan.

    Mirrors run_validation.load_target_tables so matching never disagrees.
    """
    path = state_dir / "translation_plan.json"
    if not path.exists():
        return []
    plan = json.loads(path.read_text(encoding="utf-8"))
    tables: set[str] = set()
    for target in plan.get("targets", []):
        for t in target.get("output_tables", []) or []:
            tables.add(t.split(".")[-1].upper())
    return sorted(tables)


def stage_references(state_dir: Path, input_dir: Path, ref_dir: Path) -> list[str]:
    """Copia a ``ref_dir`` las referencias SAS que matchean tablas destino.

    Devuelve las líneas 'origen -> destino' (vacía si no hay tablas o
    candidatos). Vía in-process para el nodo de Fase 7.
    """
    targets = load_target_tables(state_dir)
    if not targets:
        return []
    ref_dir.mkdir(parents=True, exist_ok=True)

    candidates: dict[str, Path] = {}
    if input_dir.exists():
        for f in sorted(input_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in REFERENCE_SUFFIXES:
                candidates.setdefault(f.stem.upper(), f)

    staged: list[str] = []
    for table in targets:
        src = candidates.get(table)
        if src is None:
            continue
        dest = ref_dir / f"{table}{src.suffix.lower()}"
        shutil.copyfile(src, dest)  # byte copy: preserve original separator/encoding
        staged.append(f"{src.name} -> {dest.name}")
    return staged


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage SAS reference outputs for validation")
    parser.add_argument("--state-dir", default="state", help="State directory (default: state)")
    parser.add_argument("--input-dir", default="input/data", help="Where SAS reference files live (default: input/data)")
    parser.add_argument("--reference-dir", default="state/reference_outputs",
                        help="Where to stage matched references (default: state/reference_outputs)")
    args = parser.parse_args()

    state_dir = Path(args.state_dir)
    input_dir = Path(args.input_dir)
    ref_dir = Path(args.reference_dir)

    targets = load_target_tables(state_dir)
    if not targets:
        print("No hay tablas destino en translation_plan.json — nada que referenciar.", file=sys.stderr)
        return 0

    staged = stage_references(state_dir, input_dir, ref_dir)

    candidate_stems: set[str] = set()
    if input_dir.exists():
        candidate_stems = {
            f.stem.upper()
            for f in input_dir.iterdir()
            if f.is_file() and f.suffix.lower() in REFERENCE_SUFFIXES
        }
    matched_tables = {s.split(' -> ')[1].rsplit('.', 1)[0] for s in staged}
    tables_without_ref = [t for t in targets if t not in matched_tables]
    files_unused = sorted(candidate_stems - set(matched_tables))

    print(f"OK referencias: {len(staged)}/{len(targets)} tablas destino con referencia SAS -> {ref_dir}/")
    for line in staged:
        print(f"  - {line}")
    if tables_without_ref:
        print(f"  Sin referencia ({len(tables_without_ref)}): {', '.join(tables_without_ref)}")
    if files_unused:
        print(f"  Archivos en {input_dir} sin tabla destino: {', '.join(files_unused)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
