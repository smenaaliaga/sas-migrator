#!/usr/bin/env python3
"""Run validation tests comparing reference (SAS) outputs to the SQL Server
target tables produced by the migrated notebooks.

The reference side are CSV files (SAS ground truth) in --reference-dir.
The generated side is read from SQL Server: for each target table in
state/translation_plan.json → targets[].output_tables, the script resolves its
connection from state/db_connections.yaml and reads the table with pd.read_sql.

Usage:
    python run_validation.py --state-dir state/ \\
                             --reference-dir state/reference_outputs/ \\
                             [--targets EJECUCION,ENTIDADES] \\
                             [--where "AÑO = :year AND MES = :mes" --param year=2026 --param mes=1] \\
                             --output state/validation_report.json

Exit codes:
    0 = validation ran (report written; check report for PASS/FAIL)  — also for not_applicable
    1 = at least one table failed validation
    3 = no database access (validation blocked/pending)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


import numpy as np
import pandas as pd
import yaml

# CSV separator for reference files. Configurable via --separator (default ";").
_CSV_SEP: str = ";"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

from sas_migrator.core.db.engine import build_engine  # noqa: E402


def load_reference(path: Path) -> Optional[pd.DataFrame]:
    """Load a reference file (SAS output) into a DataFrame."""
    ext = path.suffix.lower()
    try:
        if ext in (".csv", ".txt", ".tsv"):
            sep = "\t" if ext == ".tsv" else _CSV_SEP
            for enc in ("utf-8", "utf-8-sig", "latin-1", "cp1252"):
                try:
                    return pd.read_csv(path, encoding=enc, sep=sep, low_memory=False)
                except UnicodeDecodeError:
                    continue
        elif ext in (".xlsx", ".xls"):
            return pd.read_excel(path, sheet_name=0)
    except Exception:
        return None
    return None


def load_connections(state_dir: Path) -> list[dict]:
    path = state_dir / "db_connections.yaml"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("connections", [])


def load_target_tables(state_dir: Path) -> list[str]:
    """Extract all output tables from the translation plan."""
    path = state_dir / "translation_plan.json"
    if not path.exists():
        return []
    plan = json.loads(path.read_text(encoding="utf-8"))
    tables: list[str] = []
    for target in plan.get("targets", []):
        for t in target.get("output_tables", []) or []:
            # Normalize "LIB.TABLE" → "TABLE"
            tables.append(t.split(".")[-1].upper())
    return sorted(set(tables))




def resolve_connection(table: str, connections: list[dict]) -> Optional[dict]:
    """Find the connection that owns a table (by tables list, then by role)."""
    for conn in connections:
        if table.upper() in {t.upper() for t in conn.get("tables", [])}:
            return conn
    for conn in connections:
        if conn.get("role") in ("target", "both"):
            return conn
    return connections[0] if connections else None


def read_target_table(
    table: str,
    conn_cfg: dict,
    where: Optional[str],
    params: Dict[str, Any],
) -> pd.DataFrame:
    """Read a target table from SQL Server, with an optional parameterized filter."""
    from sqlalchemy import text
    engine = build_engine(conn_cfg)
    schema = conn_cfg.get("schema_name", "dbo")
    sql = f"SELECT * FROM [{schema}].[{table}]"
    if where:
        sql += f" WHERE {where}"  # el WHERE usa :params; los valores nunca se interpolan
    return pd.read_sql_query(text(sql), con=engine, params=params or {})


# ---------------------------------------------------------------------------
# Cascade (levels 1-5) — comparison logic unchanged
# ---------------------------------------------------------------------------

def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize column names: strip BOM, fix encoding artifacts."""
    new_cols = [c.replace('\ufeff', '').replace('ï»¿', '').strip() for c in df.columns]
    fixed = []
    for c in new_cols:
        try:
            fixed.append(c.encode('latin-1').decode('utf-8'))
        except (UnicodeDecodeError, UnicodeEncodeError):
            fixed.append(c)
    df = df.copy()
    df.columns = fixed
    return df


def compare_schemas(ref: pd.DataFrame, gen: pd.DataFrame) -> Dict[str, Any]:
    """Level 1: Check column presence and dtype compatibility."""
    ref = _normalize_columns(ref)
    gen = _normalize_columns(gen)
    ref_cols = set(ref.columns)
    gen_cols = set(gen.columns)
    missing = ref_cols - gen_cols
    extra = gen_cols - ref_cols
    return {
        "test": "schema",
        "level": 1,
        "passed": len(missing) == 0,
        "missing_columns": sorted(missing),
        "extra_columns": sorted(extra),
    }


def compare_row_count(ref: pd.DataFrame, gen: pd.DataFrame, tolerance_pct: float = 0.0) -> Dict[str, Any]:
    """Level 2: Check row counts match."""
    ref_count = len(ref)
    gen_count = len(gen)
    diff = abs(ref_count - gen_count)
    threshold = int(ref_count * tolerance_pct) if tolerance_pct > 0 else 0
    return {
        "test": "row_count",
        "level": 2,
        "passed": diff <= threshold,
        "reference_rows": ref_count,
        "generated_rows": gen_count,
        "difference": diff,
    }


def compare_values(ref: pd.DataFrame, gen: pd.DataFrame, tolerance: float = 1e-6) -> Dict[str, Any]:
    """Level 4: Cell-by-cell value comparison for shared columns.

    Both DataFrames are type-normalized and sorted by all shared columns
    before comparison to avoid false mismatches due to row ordering or
    int-vs-float formatting differences.
    """
    ref = _normalize_columns(ref)
    gen = _normalize_columns(gen)

    shared_cols = sorted(set(ref.columns) & set(gen.columns))
    if not shared_cols:
        return {"test": "value_comparison", "level": 4, "passed": False, "message": "No shared columns"}

    min_rows = min(len(ref), len(gen))
    ref_sub = ref[shared_cols].head(min_rows).copy()
    gen_sub = gen[shared_cols].head(min_rows).copy()

    # Normalize types: coerce numeric columns in both to float for consistent sort & comparison
    for col in shared_cols:
        r_num = pd.to_numeric(ref_sub[col], errors="coerce")
        g_num = pd.to_numeric(gen_sub[col], errors="coerce")
        if r_num.notna().mean() > 0.5 and g_num.notna().mean() > 0.5:
            ref_sub[col] = r_num
            gen_sub[col] = g_num
        else:
            ref_sub[col] = ref_sub[col].astype(str).str.strip().str.lower()
            gen_sub[col] = gen_sub[col].astype(str).str.strip().str.lower()

    # Choose sort columns: KEY columns (string + low-cardinality numeric) to avoid
    # positional mismatches caused by tiny value differences in high-cardinality columns
    sort_cols = []
    for col in shared_cols:
        if not pd.api.types.is_numeric_dtype(ref_sub[col]):
            sort_cols.append(col)
        elif ref_sub[col].nunique() < 1000:
            sort_cols.append(col)
    if not sort_cols:
        sort_cols = shared_cols

    try:
        ref_sub = ref_sub.sort_values(by=sort_cols, ignore_index=True, na_position="last")
        gen_sub = gen_sub.sort_values(by=sort_cols, ignore_index=True, na_position="last")
    except TypeError:
        ref_sub = ref_sub.sort_values(by=sort_cols, key=lambda s: s.astype(str), ignore_index=True)
        gen_sub = gen_sub.sort_values(by=sort_cols, key=lambda s: s.astype(str), ignore_index=True)

    mismatches = []
    for col in shared_cols:
        r = ref_sub[col]
        g = gen_sub[col]
        if pd.api.types.is_numeric_dtype(r) and pd.api.types.is_numeric_dtype(g):
            both_valid = r.notna() & g.notna()
            if both_valid.any():
                diffs = (r[both_valid] - g[both_valid]).abs()
                max_diff = float(diffs.max())
                if max_diff > tolerance:
                    mismatches.append({
                        "column": col,
                        "type": "numeric",
                        "max_diff": max_diff,
                        "mean_diff": float(diffs.mean()),
                        "rows_affected": int((diffs > tolerance).sum()),
                    })
            null_mismatch = int((r.isna() != g.isna()).sum())
            if null_mismatch > 0:
                mismatches.append({
                    "column": col,
                    "type": "null_pattern",
                    "rows_affected": null_mismatch,
                })
        else:
            diff_count = int((r != g).sum())
            if diff_count > 0:
                mismatches.append({
                    "column": col,
                    "type": "string",
                    "rows_affected": diff_count,
                })

    return {
        "test": "value_comparison",
        "level": 4,
        "passed": len(mismatches) == 0,
        "columns_checked": len(shared_cols),
        "mismatches": mismatches,
    }


def compare_aggregates(ref: pd.DataFrame, gen: pd.DataFrame, tolerance: float = 1e-6) -> Dict[str, Any]:
    """Level 5: Compare aggregate statistics."""
    ref = _normalize_columns(ref)
    gen = _normalize_columns(gen)
    shared_cols = sorted(set(ref.columns) & set(gen.columns))
    numeric_cols = [c for c in shared_cols
                    if pd.api.types.is_numeric_dtype(ref[c]) and pd.api.types.is_numeric_dtype(gen[c])]

    agg_mismatches = []
    for col in numeric_cols:
        for agg_name in ("sum", "mean", "min", "max"):
            r_val = float(getattr(ref[col], agg_name)()) if not ref[col].isna().all() else None
            g_val = float(getattr(gen[col], agg_name)()) if not gen[col].isna().all() else None
            if r_val is not None and g_val is not None:
                diff = abs(r_val - g_val)
                rel_diff = diff / max(abs(r_val), 1e-15)
                if rel_diff > tolerance:
                    agg_mismatches.append({
                        "column": col,
                        "aggregate": agg_name,
                        "reference": r_val,
                        "generated": g_val,
                        "relative_diff": rel_diff,
                    })

    return {
        "test": "aggregate_comparison",
        "level": 5,
        "passed": len(agg_mismatches) == 0,
        "columns_checked": len(numeric_cols),
        "mismatches": agg_mismatches,
    }


def validate_table(
    table: str,
    ref_path: Path,
    conn_cfg: dict,
    where: Optional[str],
    params: Dict[str, Any],
    value_tolerance: float,
    row_tolerance: float,
) -> Dict[str, Any]:
    """Validate one target table against its reference file."""
    schema = conn_cfg.get("schema_name", "dbo")
    database = conn_cfg.get("database", "")
    result: Dict[str, Any] = {
        "reference_file": str(ref_path),
        "target_table": f"{database}.{schema}.{table}",
        "tests": [],
        "overall_status": "PASS",
    }

    ref_df = load_reference(ref_path)
    if ref_df is None:
        result["overall_status"] = "ERROR"
        result["message"] = f"Cannot read reference file: {ref_path}"
        return result

    gen_df = read_target_table(table, conn_cfg, where, params)

    hard_gate_tests = [
        lambda: compare_schemas(ref_df, gen_df),
        lambda: compare_row_count(ref_df, gen_df, tolerance_pct=row_tolerance),
    ]
    soft_tests = [
        lambda: compare_aggregates(ref_df, gen_df, value_tolerance),
        lambda: compare_values(ref_df, gen_df, value_tolerance),
    ]

    for test_fn in hard_gate_tests:
        test_result = test_fn()
        result["tests"].append(test_result)
        if not test_result["passed"]:
            result["overall_status"] = "FAIL"
            return result

    for test_fn in soft_tests:
        test_result = test_fn()
        result["tests"].append(test_result)
        if not test_result["passed"]:
            result["overall_status"] = "FAIL"

    return result


def main() -> None:
    global _CSV_SEP

    parser = argparse.ArgumentParser(description="Validate SQL Server target tables vs SAS references")
    parser.add_argument("--state-dir", default="state/", help="State directory (default: state/)")
    parser.add_argument("--reference-dir", default="state/reference_outputs/",
                        help="Directory with reference (SAS) outputs")
    parser.add_argument("--targets", default=None,
                        help="Comma-separated table names to validate (default: all output_tables from the plan)")
    parser.add_argument("--where", default=None,
                        help="Optional parameterized filter, e.g. \"AÑO = :year AND MES = :mes\"")
    parser.add_argument("--param", action="append", default=[],
                        help="Filter parameter as name=value (repeatable). Numeric values are auto-cast.")
    parser.add_argument("--separator", default=";", help="CSV separator for reference files (default ';')")
    parser.add_argument("--row-tolerance", type=float, default=0.001,
                        help="Row count tolerance as fraction (default: 0.001 = 0.1%%)")
    parser.add_argument("--value-tolerance", type=float, default=1e-6,
                        help="Numeric value tolerance (default: 1e-6)")
    parser.add_argument("--output", default="state/validation_report.json", help="Output report path")
    args = parser.parse_args()

    _CSV_SEP = args.separator
    state_dir = Path(args.state_dir)
    ref_dir = Path(args.reference_dir)
    os.makedirs(Path(args.output).parent, exist_ok=True)

    params: Dict[str, Any] = {}
    for p in args.param:
        name, _, value = p.partition("=")
        try:
            params[name] = int(value)
        except ValueError:
            try:
                params[name] = float(value)
            except ValueError:
                params[name] = value

    def write_report(report: dict) -> None:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

    # --- References available? ---
    if not ref_dir.exists() or not any(ref_dir.iterdir()):
        print(f"No references found in {ref_dir}. Nothing to validate — exiting OK.")
        write_report({
            "validation_mode": "not_applicable",
            "total_tables": 0, "passed": 0, "failed": 0, "errors": 0,
            "note": f"Reference directory '{ref_dir}' is empty or does not exist.",
        })
        sys.exit(0)

    # --- Connections available? ---
    connections = load_connections(state_dir)
    if not connections:
        print("No db_connections.yaml — validation requires SQL Server target tables.")
        write_report({
            "validation_mode": "blocked",
            "total_tables": 0, "passed": 0, "failed": 0, "errors": 0,
            "note": "state/db_connections.yaml missing; cannot read target tables.",
        })
        sys.exit(3)

    # --- Resolve target tables ---
    all_targets = load_target_tables(state_dir)
    if args.targets:
        requested = {t.strip().upper() for t in args.targets.split(",")}
        targets = [t for t in all_targets if t in requested] or sorted(requested)
    else:
        targets = all_targets

    ref_files = {f.stem.upper(): f for f in ref_dir.iterdir() if f.is_file()}
    matched = [t for t in targets if t in ref_files]
    unmatched_targets = sorted(set(targets) - set(matched))
    unmatched_refs = sorted(set(ref_files) - set(targets))

    results: List[Dict[str, Any]] = []
    for table in matched:
        conn_cfg = resolve_connection(table, connections)
        print(f"Validating: {table} (vs {ref_files[table].name})")
        try:
            results.append(validate_table(
                table, ref_files[table], conn_cfg,
                args.where, params,
                value_tolerance=args.value_tolerance,
                row_tolerance=args.row_tolerance,
            ))
        except Exception as e:
            msg = str(e)
            if any(k in msg.lower() for k in ("login", "connect", "network", "timeout", "driver")):
                print(f"✗ Sin acceso a la BD: {msg}")
                write_report({
                    "validation_mode": "blocked",
                    "total_tables": len(matched), "passed": 0, "failed": 0, "errors": 0,
                    "note": f"No database access while reading {table}: {msg}",
                })
                sys.exit(3)
            results.append({
                "target_table": table,
                "reference_file": str(ref_files[table]),
                "overall_status": "ERROR",
                "message": msg,
                "tests": [],
            })

    passed = sum(1 for r in results if r["overall_status"] == "PASS")
    failed = sum(1 for r in results if r["overall_status"] == "FAIL")
    errors = sum(1 for r in results if r["overall_status"] == "ERROR")

    write_report({
        "validation_mode": "full",
        "separator": _CSV_SEP,
        "row_tolerance": args.row_tolerance,
        "value_tolerance": args.value_tolerance,
        "where": args.where,
        "total_tables": len(matched),
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "pass_rate": round(passed / max(len(matched), 1), 3),
        "unvalidated_targets": unmatched_targets,
        "unmatched_references": unmatched_refs,
        "results": results,
    })

    print(f"\nValidation complete: {passed} passed, {failed} failed, {errors} errors")
    print(f"Report: {args.output}")
    sys.exit(0 if failed == 0 and errors == 0 else 1)


if __name__ == "__main__":
    main()
