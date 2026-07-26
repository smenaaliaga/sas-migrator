#!/usr/bin/env python3
"""Profile tabular data files — invoked by agents to profile samples.

Usage:
    python profile_data.py <data_dir> [--output-dir state/]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


import pandas as pd

try:
    import openpyxl
except ImportError:
    openpyxl = None  # type: ignore


def profile_file(file_path: Path) -> dict:
    """Profile a single tabular file and detect human artifacts."""
    ext = file_path.suffix.lower()
    profile: dict = {
        "file_path": str(file_path),
        "file_type": ext.lstrip("."),
        "row_count": 0,
        "column_count": 0,
        "columns": [],
        "sheets": [],
        "encoding": None,
        "artifacts": [],
    }

    df = None
    if ext in (".csv", ".txt", ".tsv"):
        for enc in ("utf-8", "latin-1", "cp1252"):
            try:
                sep = "\t" if ext == ".tsv" else ","
                df = pd.read_csv(file_path, encoding=enc, sep=sep, nrows=50000)
                profile["encoding"] = enc
                break
            except (UnicodeDecodeError, pd.errors.ParserError):
                continue
    elif ext in (".xlsx", ".xls"):
        try:
            xls = pd.ExcelFile(file_path)
            profile["sheets"] = xls.sheet_names
            if len(xls.sheet_names) > 1:
                profile["artifacts"].append({
                    "artifact_type": "multiple_sheets",
                    "description": f"Excel file has {len(xls.sheet_names)} sheets: {xls.sheet_names}",
                    "location": "workbook",
                })
            df = pd.read_excel(file_path, sheet_name=0, nrows=50000)
            # Check for merged cells
            if openpyxl:
                wb = openpyxl.load_workbook(file_path, data_only=True)
                ws = wb.active
                if ws and ws.merged_cells.ranges:
                    profile["artifacts"].append({
                        "artifact_type": "merged_cells",
                        "description": f"{len(ws.merged_cells.ranges)} merged cell ranges detected",
                        "location": ws.title or "Sheet1",
                    })
                wb.close()
        except Exception as e:
            profile["artifacts"].append({
                "artifact_type": "other",
                "description": f"Error reading Excel: {e}",
            })
    elif ext == ".parquet":
        try:
            df = pd.read_parquet(file_path)
        except Exception as e:
            profile["artifacts"].append({
                "artifact_type": "other",
                "description": f"Error reading parquet: {e}",
            })

    if df is not None:
        profile["row_count"] = len(df)
        profile["column_count"] = len(df.columns)
        for col in df.columns:
            s = df[col]
            col_info = {
                "name": str(col),
                "dtype": str(s.dtype),
                "null_count": int(s.isnull().sum()),
                "null_pct": round(float(s.isnull().mean()) * 100, 2),
                "unique_count": int(s.nunique()),
                "sample_values": [str(v) for v in s.dropna().head(5).tolist()],
            }
            if pd.api.types.is_numeric_dtype(s):
                col_info["min_val"] = str(s.min()) if not s.isnull().all() else None
                col_info["max_val"] = str(s.max()) if not s.isnull().all() else None
                col_info["mean_val"] = round(float(s.mean()), 4) if not s.isnull().all() else None
            profile["columns"].append(col_info)

        # Detect sentinel values
        sentinel_patterns = {"-999", "N/A", "ND", "S/I", "#N/A", "...", "-", "n/a", "NA"}
        for col in df.select_dtypes(include="object").columns:
            found = set(df[col].dropna().unique()) & sentinel_patterns
            if found:
                profile["artifacts"].append({
                    "artifact_type": "sentinel_values",
                    "description": f"Column '{col}' contains sentinel values: {found}",
                    "location": str(col),
                })

        # Detect pivoted layout (columns that look like dates/months)
        import re
        date_like = [c for c in df.columns if re.match(r"(Ene|Feb|Mar|Abr|May|Jun|Jul|Ago|Sep|Oct|Nov|Dic|Jan|Apr|Aug|Dec|Q[1-4]|\d{4})", str(c), re.I)]
        if len(date_like) > 3:
            profile["artifacts"].append({
                "artifact_type": "pivoted_layout",
                "description": f"Columns appear to be date/period headers: {date_like[:5]}",
                "location": "column headers",
            })

    return profile


def main() -> None:
    parser = argparse.ArgumentParser(description="Profile tabular data files")
    parser.add_argument("data_dir", help="Directory containing data files")
    parser.add_argument("--output-dir", default="state", help="Output directory")
    args = parser.parse_args()

    data_path = Path(args.data_dir)
    out_path = Path(args.output_dir)
    os.makedirs(out_path, exist_ok=True)

    extensions = {".csv", ".xlsx", ".xls", ".parquet", ".tsv", ".txt"}
    files = [f for f in data_path.rglob("*") if f.suffix.lower() in extensions and not f.name.startswith("~")]

    profiles = []
    for f in sorted(files):
        print(f"Profiling: {f}")
        profiles.append(profile_file(f))

    report_path = out_path / "profile_report.json"
    with open(report_path, "w", encoding="utf-8") as fout:
        json.dump(profiles, fout, indent=2, ensure_ascii=False)
    print(f"\nProfile report written to {report_path} ({len(profiles)} files)")


if __name__ == "__main__":
    main()
