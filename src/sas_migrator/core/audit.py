#!/usr/bin/env python3
"""Audit SAS to Python node translation coverage and semantic risks.

This script checks:
1) Structural coverage: every non-ignored node has a mapping, notebook exists,
   cell exists. Nodes excluded by the user (state/ignored_nodes.yaml or
   translation_plan.ignored_nodes) are reported but never block coverage.
2) Traceability anchors: notebook contains node label or homolog marker.
3) Semantic heuristics for high-risk constructs (e.g. PROC HTTP). Project-specific
   markers are configurable via state/audit_heuristics.yaml (optional).

Outputs:
- state/node_translation_audit.json
- state/node_translation_audit.md

Usage:
    python .github/skills/sas-code-analysis/scripts/audit_node_translation.py --state-dir state --output-dir output
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class Issue:
    severity: str  # high, medium, low
    category: str
    node_id: str
    node_label: str
    notebook_path: str
    detail: str


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# Heurísticas dependientes de dominio — neutras por default y sobreescribibles
# vía state/audit_heuristics.yaml (o project_config.yaml: sección audit). Los
# chequeos que dependen de marcadores vacíos simplemente no aplican; los
# genéricos (PROC HTTP→requests, IMPORT→read_csv, secretos) no dependen de esto.
DEFAULT_HEURISTICS: dict[str, list[str]] = {
    "domain_markers": [],
    "sql_engine_markers": [],
    "sql_from_markers": [],
    "env_secret_markers": ["os.environ", "dotenv"],
    "runtime_df_checks": [],
}


def load_heuristics(state_dir: Path) -> dict[str, list[str]]:
    heuristics = dict(DEFAULT_HEURISTICS)

    # Capa 1: project_config.yaml del workspace (sección audit).
    try:
        from sas_migrator.core.config import load_project_config

        audit_cfg = load_project_config(state_dir.resolve().parent).audit
        for key in heuristics:
            values = getattr(audit_cfg, key, None)
            if values:
                heuristics[key] = [str(x) for x in values]
    except Exception:
        pass  # config opcional

    # Capa 2: state/audit_heuristics.yaml pisa lo anterior clave por clave.
    config_path = state_dir / "audit_heuristics.yaml"
    if not config_path.exists():
        return heuristics
    try:
        import yaml

        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        for key in heuristics:
            if isinstance(data.get(key), list):
                heuristics[key] = [str(x) for x in data[key]]
    except Exception:
        pass  # config opcional: si no se puede leer, valen las capas previas
    return heuristics


def load_ignored_nodes(state_dir: Path) -> set[str]:
    """Nodos excluidos por el usuario: ignored_nodes.yaml ∪ plan.ignored_nodes."""
    ignored: set[str] = set()

    def _collect(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("ignored_nodes", "ignored", "nodes", "node_ids"):
                if isinstance(value.get(key), list):
                    _collect(value[key])
                    return
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    ignored.add(item.strip())
                elif isinstance(item, dict):
                    node_id = str(item.get("node_id") or item.get("id") or "").strip()
                    if node_id:
                        ignored.add(node_id)

    yaml_path = state_dir / "ignored_nodes.yaml"
    if yaml_path.exists():
        try:
            import yaml

            _collect(yaml.safe_load(yaml_path.read_text(encoding="utf-8")))
        except Exception:
            pass  # si el YAML es ilegible, el plan sigue siendo fuente válida

    plan_path = state_dir / "translation_plan.json"
    if plan_path.exists():
        try:
            _collect(load_json(plan_path).get("ignored_nodes", []))
        except Exception:
            pass

    return ignored


def cells_text(nb: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for cell in nb.get("cells", []):
        src = cell.get("source", [])
        if isinstance(src, list):
            out.append("".join(src))
        else:
            out.append(str(src))
    return out


def find_features_sas(sas_code: str, heuristics: dict[str, list[str]]) -> dict[str, bool]:
    low = sas_code.lower()
    return {
        "proc_http": bool(re.search(r"\bPROC\s+HTTP\b", sas_code, flags=re.IGNORECASE)),
        "si3_url": any(marker in low for marker in heuristics["domain_markers"]),
        "macro_user_pass": bool(re.search(r"&(user|password)\b", sas_code, flags=re.IGNORECASE)),
        "proc_import": bool(re.search(r"\bPROC\s+IMPORT\b", sas_code, flags=re.IGNORECASE)),
        # Semántica de escritura del SAS original (espejo, no invención):
        "db_append": bool(re.search(
            r"\bPROC\s+APPEND\b|\bAPPEND\s+BASE\s*=|\bINSERT\s+INTO\b",
            sas_code, flags=re.IGNORECASE,
        )),
        "db_delete": bool(re.search(r"\bDELETE\s+FROM\b", sas_code, flags=re.IGNORECASE)),
        "sql_create_table": bool(re.search(
            r"\bCREATE\s+TABLE\b", sas_code, flags=re.IGNORECASE
        )),
        "proc_compare": bool(re.search(r"\bPROC\s+COMPARE\b", sas_code, flags=re.IGNORECASE)),
        "proc_gplot": bool(re.search(r"\bPROC\s+GPLOT\b", sas_code, flags=re.IGNORECASE)),
        "proc_gchart": bool(re.search(r"\bPROC\s+GCHART\b", sas_code, flags=re.IGNORECASE)),
        "proc_template": bool(re.search(r"\bPROC\s+TEMPLATE\b", sas_code, flags=re.IGNORECASE)),
    }


def find_features_py(py_text: str, heuristics: dict[str, list[str]]) -> dict[str, bool]:
    low = py_text.lower()
    return {
        "requests_like": ("requests." in low) or ("httpx." in low) or ("urllib" in low),
        "env_secret": any(marker in low for marker in heuristics["env_secret_markers"]),
        "sql_si3": (
            any(marker in low for marker in heuristics["sql_engine_markers"])
            and ("read_sql" in low)
        )
        or any(marker in low for marker in heuristics["sql_from_markers"]),
        "read_file": ("read_excel(" in low) or ("read_csv(" in low),
        "delete_from": "delete from" in low,
        "append_write": ('if_exists="append"' in low) or ("if_exists='append'" in low)
        or ("insert into" in low),
        "select_into": bool(re.search(r"\bselect\b[\s\S]*\binto\s+[\w.\[\]]+", low)),
        "compare_like": (
            ("compare" in low)
            or ("assert" in low)
            or ("mismatch" in low)
            or ("diff" in low)
            or ("brecha" in low)
        ),
        "plot_like": (
            ("matplotlib" in low)
            or ("plt." in low)
            or ("plot(" in low)
            or ("bar(" in low)
            or ("lineplot" in low)
            or ("scatter" in low)
        ),
        "homolog_marker": ("homologo sas" in low) or ("homologo" in low),
    }


def _issue_list_to_md(issues: list[Issue]) -> str:
    lines: list[str] = []
    if issues:
        lines.append("## Top issues (first 30)")
        lines.append("")
        for i in issues[:30]:
            lines.append(
                f"- [{i.severity.upper()}] {i.node_id} ({i.node_label}) @ {i.notebook_path}: {i.detail}"
            )
    else:
        lines.append("No issues found.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit node translation coverage")
    parser.add_argument("--state-dir", default="state")
    parser.add_argument("--output-dir", default="output")
    args = parser.parse_args()

    root = Path.cwd()
    return run_audit((root / args.state_dir).resolve(), (root / args.output_dir).resolve())


# ── Reglas placement-aware (Etapa 4) ────────────────────────────────────────

_DATA_IO_TOKENS = ("read_sql", "read_csv", "read_excel", "to_sql")


def _placement_issues(
    placement: str | None, nid: str, node_label: str, nb_rel: str, mapped_text: str
) -> list[Issue]:
    """La traducción debe respetar DÓNDE corre el cómputo (placement).

    Heurísticas sobre la(s) celda(s) mapeadas del nodo:
    - sql_pushdown: full-table read (read_sql sin WHERE) + pandas pesado = high;
    - pandas: SQL dinámico (f-string/.format) dentro de read_sql = high;
    - hybrid: el extract debe filtrar en el WHERE = medium;
    - utility: no debería tener I/O de datos = low.
    """
    text = mapped_text or ""
    low = text.lower()
    if not placement or not text:
        return []

    def issue(severity: str, detail: str) -> Issue:
        return Issue(severity, "placement", nid, node_label, nb_rel, detail)

    has_read_sql = "read_sql" in low
    has_where = "where" in low
    if placement == "sql_pushdown":
        if has_read_sql and not has_where and (".groupby(" in text or ".merge(" in text):
            return [issue(
                "high",
                "sql_pushdown: full-table read (read_sql sin WHERE) con groupby/merge "
                "pesado en pandas — el cómputo debe ir en el SQL",
            )]
    elif placement == "pandas":
        if has_read_sql and ('f"' in text or "f'" in text or ".format(" in text):
            return [issue(
                "high",
                "pandas: SQL dinámico (f-string/.format) en read_sql — un nodo pandas "
                "no arma SQL dinámico",
            )]
    elif placement == "hybrid":
        if has_read_sql and not has_where:
            return [issue(
                "medium",
                "hybrid: read_sql sin WHERE — el extract debe filtrar al mínimo en el SQL",
            )]
    elif placement == "utility":
        if any(tok in low for tok in _DATA_IO_TOKENS):
            return [issue(
                "low",
                "utility: nodo utilitario con I/O de datos — revisar si el placement "
                "es correcto",
            )]
    return []


def run_audit(state_dir: Path, output_dir: Path) -> int:
    """Corre la auditoría in-process (invocable desde check_gate sin subprocess)."""
    state_dir = Path(state_dir).resolve()
    _output_dir = Path(output_dir).resolve()
    # Los notebook_path del mapping son relativos a la raíz del workspace.
    root = state_dir.parent

    nodes_index = load_json(state_dir / "nodes_index.json")
    mapping_doc = load_json(state_dir / "sas_python_mapping.json")
    heuristics = load_heuristics(state_dir)
    ignored = load_ignored_nodes(state_dir)

    # Placement efectivo (clasificador + overrides de la entrevista B4b).
    from sas_migrator.core.planning import load_placement_overrides

    placement_overrides = load_placement_overrides(state_dir)

    nodes = nodes_index.get("nodes", [])
    mappings = mapping_doc.get("mappings", mapping_doc if isinstance(mapping_doc, list) else [])

    node_by_id = {n.get("id"): n for n in nodes}
    map_by_id = {m.get("node_id"): m for m in mappings}

    issues: list[Issue] = []

    # Los nodos ignorados por el usuario (entrevista B2) no exigen mapping,
    # pero se listan en el reporte para auditoría humana.
    ignored_present = sorted(ignored & set(node_by_id))
    missing_mapping = sorted(
        [nid for nid in node_by_id if nid not in map_by_id and nid not in ignored]
    )
    extra_mapping = sorted([nid for nid in map_by_id if nid not in node_by_id])

    for nid in missing_mapping:
        n = node_by_id[nid]
        issues.append(
            Issue(
                severity="high",
                category="coverage",
                node_id=nid,
                node_label=n.get("label", ""),
                notebook_path="",
                detail="Node exists in nodes_index but has no entry in sas_python_mapping",
            )
        )

    for nid in extra_mapping:
        m = map_by_id[nid]
        issues.append(
            Issue(
                severity="medium",
                category="coverage",
                node_id=nid,
                node_label=m.get("node_label", ""),
                notebook_path=m.get("notebook_path", ""),
                detail="Mapping exists but node_id is not present in nodes_index",
            )
        )

    # Cache: un mismo notebook contiene decenas de mappings — parsearlo una vez.
    nb_cache: dict[str, dict[str, Any]] = {}

    def _load_notebook(path: Path) -> dict[str, Any]:
        key = str(path)
        if key not in nb_cache:
            nb_cache[key] = load_json(path)
        return nb_cache[key]

    for nid, m in map_by_id.items():
        node = node_by_id.get(nid)
        nb_rel = m.get("notebook_path", "")
        nb_path = (root / nb_rel).resolve() if nb_rel else None
        node_label = m.get("node_label", node.get("label", "") if node else "")

        if not nb_path or not nb_path.exists():
            issues.append(
                Issue(
                    severity="high",
                    category="coverage",
                    node_id=nid,
                    node_label=node_label,
                    notebook_path=nb_rel,
                    detail="Mapped notebook file does not exist",
                )
            )
            continue

        nb = _load_notebook(nb_path)
        all_cells = cells_text(nb)
        all_text = "\n".join(all_cells)

        cidx = m.get("cell_index")
        mapped_text = ""
        mapped_cell_type = ""
        if isinstance(cidx, int) and 0 <= cidx < len(all_cells):
            mapped_text = all_cells[cidx]
            mapped_cell_type = nb.get("cells", [])[cidx].get("cell_type", "")
            # Many mappings currently point to the markdown header; use the next
            # code cell as effective translation anchor for semantic checks.
            if mapped_cell_type != "code" and (cidx + 1) < len(all_cells):
                next_type = nb.get("cells", [])[cidx + 1].get("cell_type", "")
                if next_type == "code":
                    mapped_text = all_cells[cidx + 1]
                    issues.append(
                        Issue(
                            severity="low",
                            category="traceability",
                            node_id=nid,
                            node_label=node_label,
                            notebook_path=nb_rel,
                            detail=(
                                f"cell_index={cidx} points to {mapped_cell_type}; "
                                f"effective code appears at cell_index={cidx+1}"
                            ),
                        )
                    )
        else:
            issues.append(
                Issue(
                    severity="high",
                    category="traceability",
                    node_id=nid,
                    node_label=node_label,
                    notebook_path=nb_rel,
                    detail=f"Invalid cell_index={cidx} (cells={len(all_cells)})",
                )
            )

        if node_label and (f"## {node_label}" not in all_text) and (f'Program "{node_label}"' not in all_text):
            issues.append(
                Issue(
                    severity="low",
                    category="traceability",
                    node_id=nid,
                    node_label=node_label,
                    notebook_path=nb_rel,
                    detail="Node label/homolog marker not found in notebook markdown",
                )
            )

        placement = placement_overrides.get(nid) or (
            node.get("placement") if node else None
        )
        issues.extend(_placement_issues(placement, nid, node_label, nb_rel, mapped_text))

        node_file = state_dir / "nodes" / f"{nid}.json"
        if not node_file.exists():
            issues.append(
                Issue(
                    severity="high",
                    category="coverage",
                    node_id=nid,
                    node_label=node_label,
                    notebook_path=nb_rel,
                    detail="Node file missing in state/nodes",
                )
            )
            continue

        node_doc = load_json(node_file)
        sas_code = str(node_doc.get("code", ""))
        sas_f = find_features_sas(sas_code, heuristics)
        py_f_cell = find_features_py(mapped_text, heuristics)
        py_f_nb = find_features_py(all_text, heuristics)

        if sas_f["proc_http"] and not (py_f_cell["requests_like"] or py_f_nb["requests_like"]):
            issues.append(
                Issue(
                    severity="high",
                    category="semantic",
                    node_id=nid,
                    node_label=node_label,
                    notebook_path=nb_rel,
                    detail=(
                        "SAS node uses PROC HTTP, but Python translation has no "
                        "requests/httpx/urllib pattern"
                    ),
                )
            )

        if (
            sas_f["proc_http"]
            and (py_f_cell["sql_si3"] or py_f_nb["sql_si3"])
            and not (py_f_cell["requests_like"] or py_f_nb["requests_like"])
        ):
            issues.append(
                Issue(
                    severity="high",
                    category="semantic",
                    node_id=nid,
                    node_label=node_label,
                    notebook_path=nb_rel,
                    detail=(
                        "PROC HTTP appears to be replaced by SQL read of SI3.BCENTRAL "
                        "(potential semantic drift)"
                    ),
                )
            )

        # Semántica de escritura: la traducción replica la del SAS — no inventa.
        if sas_f["db_append"] and not sas_f["db_delete"] and py_f_cell["delete_from"]:
            issues.append(
                Issue(
                    severity="high",
                    category="semantic",
                    node_id=nid,
                    node_label=node_label,
                    notebook_path=nb_rel,
                    detail=(
                        "El SAS original ACUMULA (PROC APPEND/INSERT INTO sin DELETE) "
                        "pero la traducción agrega DELETE FROM — cambia la semántica de "
                        "escritura. Idempotencia por periodo es una mejora M-xxx que "
                        "aprueba el usuario, no una decisión del traductor."
                    ),
                )
            )
        if (
            sas_f["sql_create_table"]
            and not sas_f["db_append"]
            and py_f_cell["append_write"]
            and not py_f_cell["delete_from"]
            and not py_f_cell["select_into"]
        ):
            issues.append(
                Issue(
                    severity="medium",
                    category="semantic",
                    node_id=nid,
                    node_label=node_label,
                    notebook_path=nb_rel,
                    detail=(
                        "El SAS original REEMPLAZA la tabla (CREATE TABLE) pero la "
                        "traducción hace append sin DELETE previo — re-ejecutar "
                        "duplicaría filas que SAS no duplicaba."
                    ),
                )
            )

        if sas_f["macro_user_pass"] and not (py_f_cell["env_secret"] or py_f_nb["env_secret"]):
            issues.append(
                Issue(
                    severity="medium",
                    category="security",
                    node_id=nid,
                    node_label=node_label,
                    notebook_path=nb_rel,
                    detail=(
                        "SAS uses &user/&password but Python translation has no "
                        "env-secret handling pattern"
                    ),
                )
            )

        if sas_f["proc_import"] and not (py_f_cell["read_file"] or py_f_nb["read_file"]):
            issues.append(
                Issue(
                    severity="medium",
                    category="semantic",
                    node_id=nid,
                    node_label=node_label,
                    notebook_path=nb_rel,
                    detail="SAS PROC IMPORT found but Python translation has no read_excel/read_csv pattern",
                )
            )

        if (sas_f["proc_gplot"] or sas_f["proc_gchart"]) and not (
            py_f_cell["plot_like"] or py_f_nb["plot_like"]
        ):
            issues.append(
                Issue(
                    severity="medium",
                    category="semantic",
                    node_id=nid,
                    node_label=node_label,
                    notebook_path=nb_rel,
                    detail="SAS graph PROC (GPLOT/GCHART) found but Python translation has no plotting pattern",
                )
            )

        if sas_f["proc_compare"] and not (py_f_cell["compare_like"] or py_f_nb["compare_like"]):
            issues.append(
                Issue(
                    severity="medium",
                    category="semantic",
                    node_id=nid,
                    node_label=node_label,
                    notebook_path=nb_rel,
                    detail="SAS PROC COMPARE found but Python translation has no compare/diff pattern",
                )
            )

        # Runtime safety check: dataframe is mutated but never initialized.
        for df_name in heuristics["runtime_df_checks"]:
            if (df_name in mapped_text) and (f"{df_name}[" in mapped_text or f"{df_name}." in mapped_text):
                has_define = bool(re.search(rf"\b{re.escape(df_name)}\s*=", all_text))
                if not has_define:
                    issues.append(
                        Issue(
                            severity="high",
                            category="runtime",
                            node_id=nid,
                            node_label=node_label,
                            notebook_path=nb_rel,
                            detail=(
                                f"{df_name} is referenced/mutated but not initialized in notebook "
                                "(risk of NameError at execution)"
                            ),
                        )
                    )

    sev_counts = {"high": 0, "medium": 0, "low": 0}
    for i in issues:
        sev_counts[i.severity] = sev_counts.get(i.severity, 0) + 1

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": {
            "nodes_total": len(node_by_id),
            "nodes_in_scope": len(node_by_id) - len(ignored_present),
            "ignored_count": len(ignored_present),
            "mappings_total": len(map_by_id),
            "missing_mapping_count": len(missing_mapping),
            "extra_mapping_count": len(extra_mapping),
            "issues_total": len(issues),
            "issues_by_severity": sev_counts,
        },
        "ignored_nodes": ignored_present,
        "issues": [asdict(i) for i in issues],
    }

    out_json = state_dir / "node_translation_audit.json"
    out_md = state_dir / "node_translation_audit.md"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines: list[str] = []
    lines.append("# Node Translation Audit")
    lines.append("")
    lines.append(f"Generated at: {report['generated_at']}")
    lines.append("")
    s = report["summary"]
    lines.append(f"- Nodes total: {s['nodes_total']}")
    lines.append(f"- Nodes in scope: {s['nodes_in_scope']} (ignored by user: {s['ignored_count']})")
    lines.append(f"- Mappings total: {s['mappings_total']}")
    lines.append(f"- Missing mapping: {s['missing_mapping_count']}")
    lines.append(f"- Extra mapping: {s['extra_mapping_count']}")
    lines.append(f"- Issues total: {s['issues_total']}")
    lines.append(
        f"- High: {s['issues_by_severity']['high']} | Medium: {s['issues_by_severity']['medium']} | Low: {s['issues_by_severity']['low']}"
    )
    lines.append("")
    if ignored_present:
        lines.append("## Ignored nodes (user decision, excluded from coverage)")
        lines.append("")
        for nid in ignored_present:
            label = node_by_id.get(nid, {}).get("label", "")
            lines.append(f"- {nid} ({label})")
        lines.append("")
    lines.append(_issue_list_to_md(issues))

    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps(report["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
