"""Schema validation utilities — validate artifacts against JSON Schema, check phase gates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import yaml

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"


def load_schema(schema_name: str) -> dict:
    """Load a JSON Schema file by name (without extension)."""
    path = SCHEMAS_DIR / f"{schema_name}.schema.json"
    if not path.exists():
        raise FileNotFoundError(f"Schema not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_artifact(data: dict | list, schema_name: str) -> list[str]:
    """Validate a dict or list against a named JSON Schema. Returns list of error messages (empty = valid).
    
    If data is a list, validates each item against the schema and returns errors for any invalid items.
    If data is a dict but looks like a wrapper (has a list under a common key), validates the list items.
    """
    schema = load_schema(schema_name)
    validator = jsonschema.Draft202012Validator(schema)

    if isinstance(data, list):
        # Validate ALL items (un elemento malformado en la posición N debe
        # fallar el gate igual que en la 1). El cap aplica solo al reporte.
        errors: list[str] = []
        for i, item in enumerate(data):
            for e in validator.iter_errors(item):
                errors.append(f"[item {i}] {e.message}")
                if len(errors) >= 20:
                    errors.append(
                        f"... (más errores omitidos del reporte; se validaron los {len(data)} items)"
                    )
                    return errors
        return errors

    # Algunos artefactos se guardan como wrappers YAML/JSON sobre listas de items.
    collection_keys_by_schema = {
        "code_smells": ("smells", "code_smells", "items"),
        "column_mapping": ("mappings", "columns", "items"),
        "file_mapping": ("mappings", "files", "items"),
        "improvements": ("improvements", "approved", "items"),
        "lineage": ("lineage", "entries", "items"),
        "preprocess_candidate": ("candidates", "items"),
        "profile_report": ("profiles", "items"),
    }
    for key in collection_keys_by_schema.get(schema_name, ()):
        if key in data and isinstance(data[key], list):
            return validate_artifact(data[key], schema_name)

    return [e.message for e in validator.iter_errors(data)]


def validate_artifact_file(artifact_path: str | Path, schema_name: str) -> list[str]:
    """Validate a JSON or YAML file on disk against a named JSON Schema."""
    path = Path(artifact_path)
    if not path.exists():
        return [f"Artifact file not found: {path}"]
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    return validate_artifact(data, schema_name)


# ── Gate definitions ────────────────────────────────────────────────────────

# Maps phase number → list of (artifact_file_relative_to_state, schema_name)
GATE_REQUIREMENTS: dict[int, list[tuple[str, str]]] = {
    0: [("intake.json", "intake")],
    1: [("initial_interview.yaml", "interview")],
    2: [
        ("flow_graph.json", "flow_graph"),
        ("flow_summary.json", "flow_summary"),
        ("extraction_residue.json", "extraction_residue"),
        ("lineage.json", "lineage"),
        ("code_smells.json", "code_smells"),
        ("improvements_proposed.yaml", "improvements"),
    ],
    3: [
        ("profile_report.json", "profile_report"),
        ("file_mapping.json", "file_mapping"),
    ],
    4: [
        ("post_analysis_interview.yaml", "interview"),
        ("approved_improvements.yaml", "improvements"),
    ],
    5: [("translation_plan.json", "translation_plan")],
    6: [],  # generated code — validated by phase 7
    7: [
        ("validation_report.json", "validation_report"),
        ("node_translation_audit.json", "node_translation_audit"),
    ],
    8: [],  # documentation — no schema gate
}


def _load_any(path: Path) -> Any:
    """Carga JSON o YAML de disco; None si no existe o es ilegible."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() in (".yaml", ".yml"):
            return yaml.safe_load(text)
        return json.loads(text)
    except Exception:
        return None


def _collect_node_ids(value: Any) -> set[str]:
    """IDs de nodo desde formas tolerantes: lista de str, lista de dicts, o
    wrapper con claves conocidas (ignored_nodes/ignored/nodes/node_ids)."""
    ids: set[str] = set()
    if isinstance(value, dict):
        for key in ("ignored_nodes", "ignored", "nodes", "node_ids"):
            if isinstance(value.get(key), list):
                return _collect_node_ids(value[key])
        return ids
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                ids.add(item.strip())
            elif isinstance(item, dict):
                node_id = str(item.get("node_id") or item.get("id") or "").strip()
                if node_id:
                    ids.add(node_id)
    return ids


def _ignored_node_ids(state_path: Path) -> set[str]:
    """Exclusiones del usuario: ignored_nodes.yaml ∪ translation_plan.ignored_nodes."""
    ignored = _collect_node_ids(_load_any(state_path / "ignored_nodes.yaml"))
    plan = _load_any(state_path / "translation_plan.json")
    if isinstance(plan, dict):
        ignored |= _collect_node_ids(plan.get("ignored_nodes", []))
    return ignored


def _phase2_completeness_errors(state_path: Path) -> list[str]:
    """Gate 2: el análisis debe cubrir el .egp completo, no solo existir."""
    errors: list[str] = []

    graph = _load_any(state_path / "flow_graph.json")
    graph_node_ids = {
        str(n.get("id", "")) for n in (graph or {}).get("nodes", []) if n.get("id")
    }

    nodes_dir = state_path / "nodes"
    node_files = {p.stem for p in nodes_dir.glob("*.json")} if nodes_dir.exists() else set()
    if not node_files:
        errors.append("state/nodes/ is empty or missing — no node files persisted")
    elif graph_node_ids != node_files:
        missing = sorted(graph_node_ids - node_files)[:10]
        extra = sorted(node_files - graph_node_ids)[:10]
        errors.append(
            "flow_graph nodes and state/nodes/ files do not match "
            f"(sin archivo: {missing or 'ninguno'}; sin nodo en el grafo: {extra or 'ninguno'})"
        )

    summary = _load_any(state_path / "flow_summary.json")
    if isinstance(summary, dict) and int(summary.get("migratable_flows", 0)) < 1:
        errors.append("flow_summary.json reports 0 migratable flows")

    residue = _load_any(state_path / "extraction_residue.json")
    if isinstance(residue, dict):
        unexplained = int(residue.get("unexplained_count", 0))
        if unexplained > 0:
            errors.append(
                f"extraction_residue.json reports {unexplained} unexplained item(s) — "
                "código o topología del .egp sin capturar (ver orphan_code_entries/"
                "unmaterialized_tasks/dropped_edges/warnings)"
            )

    progress = _load_any(state_path / "analysis_progress.json")
    if progress is None:
        errors.append(
            "Missing artifact: analysis_progress.json (correr analysis_ledger.py init "
            "y completar la revisión nodo por nodo)"
        )
    else:
        entries = progress.get("nodes", progress if isinstance(progress, list) else [])
        pending = [
            str(e.get("node_id", "")) for e in entries if e.get("status") != "reviewed"
        ]
        if pending:
            errors.append(
                f"analysis_progress.json has {len(pending)} node(s) pending review "
                f"(ej: {pending[:5]})"
            )

    errors.extend(_phase2_substance_errors(state_path))
    return errors


# Un análisis puede estar "completo" por conteo y vacío de contenido: nodos sin
# clasificar, flujos sin descripción, notas de review copiadas entre nodos y
# categorías declaradas limpias contra la evidencia. Estos chequeos existen
# porque el gate anterior pasaba en todos esos casos.
_BOILERPLATE_REVIEW_RATIO = 0.5


def _phase2_substance_errors(state_path: Path) -> list[str]:
    """Gate 2: el análisis debe tener contenido, no solo artefactos presentes."""
    errors: list[str] = []

    # ── Clasificación determinista presente en cada nodo ────────────────
    nodes_dir = state_path / "nodes"
    unclassified: list[str] = []
    if nodes_dir.exists():
        for node_path in sorted(nodes_dir.glob("*.json")):
            node = _load_any(node_path)
            if not isinstance(node, dict):
                continue
            meta = node.get("metadata") or {}
            if not isinstance(meta, dict) or not meta.get("classification"):
                unclassified.append(node_path.stem)
    if unclassified:
        errors.append(
            f"{len(unclassified)} node(s) sin metadata.classification "
            f"(ej: {unclassified[:5]}) — correr generate_analysis.py; si el .egp "
            "se re-extrajo después del análisis, hay que rehacerlo"
        )

    # ── Descripciones de negocio en los flujos migrables ────────────────
    summary = _load_any(state_path / "flow_summary.json")
    if isinstance(summary, dict):
        undescribed = [
            str(f.get("pfd_id", ""))
            for f in summary.get("flows", [])
            if isinstance(f, dict)
            and f.get("migratable_candidate")
            and not str(f.get("description") or "").strip()
        ]
        if undescribed:
            errors.append(
                f"{len(undescribed)} flujo(s) migrable(s) sin `description` en "
                f"flow_summary.json (ej: {undescribed[:5]}) — el code-analyst debe "
                "redactarla; la Fase 4 la presenta al usuario para elegir alcance"
            )

    # ── Notas de review con contenido propio, no plantilla repetida ──────
    reviews_dir = state_path / "analysis_reviews"
    if reviews_dir.exists():
        notes: list[str] = []
        for review_path in sorted(reviews_dir.glob("*.json")):
            review = _load_any(review_path)
            if not isinstance(review, dict):
                continue
            for item in review.get("reviews", []):
                if isinstance(item, dict):
                    note = str(item.get("note") or "").strip()
                    if note:
                        notes.append(note)
        if notes:
            distinct_ratio = len(set(notes)) / len(notes)
            if distinct_ratio < _BOILERPLATE_REVIEW_RATIO:
                errors.append(
                    f"analysis_reviews/: solo {len(set(notes))} nota(s) distinta(s) "
                    f"entre {len(notes)} — las revisiones son plantilla repetida, no "
                    "criterio por nodo. Cada nodo necesita propósito y riesgo propios"
                )

    # ── category_scan_summary coherente con la evidencia de smells ───────
    proposed = _load_any(state_path / "improvements_proposed.yaml")
    smells_doc = _load_any(state_path / "code_smells.json")
    if isinstance(proposed, dict) and isinstance(smells_doc, dict):
        scan = proposed.get("category_scan_summary") or {}
        smells = smells_doc.get("smells") or []
        categories_with_evidence = {
            str(s.get("category", "")).strip().lower()
            for s in smells
            if isinstance(s, dict) and s.get("category")
        }
        carded = {
            str(i.get("category", "")).strip().lower()
            for i in (proposed.get("improvements") or [])
            if isinstance(i, dict) and i.get("category")
        }
        contradictions = sorted(
            cat
            for cat, verdict in scan.items()
            if isinstance(verdict, str)
            and "no improvements" in verdict.lower()
            and str(cat).strip().lower() in categories_with_evidence
            and str(cat).strip().lower() not in carded
        )
        if contradictions:
            counts = {
                cat: sum(
                    1
                    for s in smells
                    if str(s.get("category", "")).strip().lower() == cat
                )
                for cat in contradictions
            }
            errors.append(
                "category_scan_summary declara 'No improvements found' en "
                f"categorías con evidencia en code_smells.json: {counts} — o se "
                "redacta una ficha M-xxx, o se justifica por qué los smells no "
                "ameritan mejora"
            )

    return errors


def _phase3_errors(state_path: Path) -> list[str]:
    """Gate 3: column_mapping es exigible cuando hubo archivos perfilados."""
    profile = _load_any(state_path / "profile_report.json")
    profiles = (
        profile if isinstance(profile, list) else (profile or {}).get("profiles", [])
    )
    if profiles and not (state_path / "column_mapping.yaml").exists():
        return [
            "Missing artifact: column_mapping.yaml (hay archivos perfilados; el escape "
            "solo aplica cuando profile_report.json no registra ninguno)"
        ]
    return []


def _phase4_errors(state_path: Path) -> list[str]:
    """Gate 4: alcance registrado y toda mejora M-xxx con decisión explícita."""
    errors: list[str] = []

    if not (state_path / "ignored_nodes.yaml").exists():
        errors.append(
            "Missing artifact: ignored_nodes.yaml (lista vacía si no se excluyó nada)"
        )

    proposed = _load_any(state_path / "improvements_proposed.yaml")
    proposed_items = (
        proposed if isinstance(proposed, list) else (proposed or {}).get("improvements", [])
    ) or []
    proposed_ids = {
        str(p.get("id", "")).strip() for p in proposed_items if isinstance(p, dict) and p.get("id")
    }

    approved = _load_any(state_path / "approved_improvements.yaml")
    approved_items = (
        approved if isinstance(approved, list) else (approved or {}).get("improvements", [])
    ) or []
    decided = {
        str(a.get("id", "")).strip(): str(a.get("status", "")).strip().lower()
        for a in approved_items
        if isinstance(a, dict) and a.get("id")
    }

    valid_status = {"approved", "rejected", "postponed"}
    undecided = sorted(
        pid for pid in proposed_ids if decided.get(pid) not in valid_status
    )
    if undecided:
        errors.append(
            f"{len(undecided)} improvement card(s) sin decisión registrada "
            f"(approved/rejected/postponed): {undecided[:10]}"
        )

    return errors


def _phase5_errors(state_path: Path) -> list[str]:
    """Gate 5: el plan cubre exactamente los nodos no ignorados y está aprobado."""
    errors: list[str] = []

    plan = _load_any(state_path / "translation_plan.json")
    if not isinstance(plan, dict):
        return ["translation_plan.json is missing or unreadable"]

    if plan.get("user_approved") is not True:
        errors.append("translation_plan.json has user_approved != true")

    index = _load_any(state_path / "nodes_index.json")
    index_ids = {
        str(n.get("id", "")) for n in (index or {}).get("nodes", []) if n.get("id")
    }
    if not index_ids:
        errors.append("nodes_index.json is missing or has no nodes (correr build_indexes.py)")
        return errors

    ignored = _ignored_node_ids(state_path)
    plan_ids = {
        str(t.get("node_id", ""))
        for t in plan.get("targets", [])
        if isinstance(t, dict) and t.get("node_id")
    }

    expected = index_ids - ignored
    missing = sorted(expected - plan_ids)
    extra = sorted(plan_ids - expected)
    if missing:
        errors.append(
            f"translation plan does not cover {len(missing)} non-ignored node(s): {missing[:10]}"
        )
    if extra:
        errors.append(
            f"translation plan includes {len(extra)} node(s) fuera del alcance "
            f"(ignorados o inexistentes): {extra[:10]}"
        )

    return errors


REQUIRED_DOCS = ("README.md", "LINEAGE.md", "DECISIONS.md", "IMPROVEMENTS.md", "RUNBOOK.md")


def _phase8_errors(state_path: Path) -> list[str]:
    """Gate 8: la documentación requerida existe y no está vacía."""
    docs_dir = state_path.parent / "output" / "docs"
    errors: list[str] = []
    for doc in REQUIRED_DOCS:
        path = docs_dir / doc
        if not path.exists():
            errors.append(f"Missing doc: output/docs/{doc}")
        elif not path.read_text(encoding="utf-8").strip():
            errors.append(f"Doc is empty: output/docs/{doc}")
    return errors


def _extract_notebook_code(nb_data: dict) -> str:
    """Return concatenated code from all code cells in a notebook JSON payload."""
    chunks: list[str] = []
    for cell in nb_data.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        src = cell.get("source", [])
        if isinstance(src, list):
            chunks.append("".join(src))
        elif isinstance(src, str):
            chunks.append(src)
    return "\n".join(chunks)


def _phase6_generation_errors(state_path: Path) -> list[str]:
    """Validate that generated notebooks are not traceability-only placeholders."""
    errors: list[str] = []
    repo_root = state_path.parent
    output_dir = repo_root / "output"

    if not output_dir.exists():
        return ["Missing generated output directory: output/"]

    notebooks = sorted(output_dir.glob("NB-*.ipynb"))
    if not notebooks:
        flow_nb = output_dir / "flow.ipynb"
        if flow_nb.exists():
            notebooks = [flow_nb]

    if not notebooks:
        errors.append("No generated notebooks found in output/ (expected NB-*.ipynb or flow.ipynb)")
        return errors

    translation_signals = (
        "pd.read_sql",
        "read_sql_query(",
        ".merge(",
        ".groupby(",
        "to_sql(",
        "read_csv(",
        "read_excel(",
        "np.where(",
        "np.select(",
        "sort_values(",
    )

    for nb in notebooks:
        try:
            nb_data = json.loads(nb.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - defensive check for malformed notebooks
            errors.append(f"Notebook is not valid JSON: {nb.name} ({exc})")
            continue

        code = _extract_notebook_code(nb_data)
        if not code.strip():
            errors.append(f"Notebook has no code cells: {nb.name}")
            continue

        has_traceability_stub = (
            ("NODE_PLAN" in code and "flow_result=pd.DataFrame(NODE_PLAN)" in code)
            or ("NODE_PLAN" in code and "flow_result = pd.DataFrame(NODE_PLAN)" in code)
            or (
                "Celda de ejecucion por nodo (trazabilidad SAS->Python)" in code
                and "for spec in NODE_PLAN" in code
                and "df = pd.DataFrame({'node_id'" in code
            )
        )
        has_translation_logic = any(token in code for token in translation_signals)

        if has_traceability_stub and not has_translation_logic:
            errors.append(
                f"Notebook appears to be traceability-only placeholder without translation logic: {nb.name}"
            )

    return errors


def _run_node_translation_audit(state_path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """Run the semantic audit in-process and validate its output artifact.

    v1 lanzaba el script por subprocess desde una ruta fija bajo .github/;
    en v2 la auditoría es un módulo del paquete y se invoca directo.
    """
    repo_root = state_path.parent
    try:
        from sas_migrator.core.audit import run_audit

        run_audit(state_path, repo_root / "output")
    except Exception as exc:
        return None, [f"Semantic audit execution failed: {exc}"]

    audit_path = state_path / "node_translation_audit.json"
    if not audit_path.exists():
        return None, ["Semantic audit did not produce state/node_translation_audit.json"]

    schema_errors = validate_artifact_file(audit_path, "node_translation_audit")
    if schema_errors:
        return None, [f"node_translation_audit.json: {e}" for e in schema_errors]

    try:
        report = json.loads(audit_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, [f"node_translation_audit.json is not valid JSON: {exc}"]

    return report, []


def _phase6_semantic_audit_errors(state_path: Path) -> list[str]:
    """Block phase 6 on high-severity semantic audit findings."""
    report, errors = _run_node_translation_audit(state_path)
    if errors:
        return errors
    if report is None:  # defensive
        return ["Semantic audit returned no report"]

    summary = report.get("summary", {})
    issues = report.get("issues", [])
    high_count = int(summary.get("issues_by_severity", {}).get("high", 0))
    missing_mapping = int(summary.get("missing_mapping_count", 0))
    markdown_mapping = sum(
        1
        for issue in issues
        if issue.get("category") == "traceability"
        and "points to markdown" in str(issue.get("detail", "")).lower()
    )

    out: list[str] = []
    if missing_mapping > 0:
        out.append(f"Semantic audit found {missing_mapping} nodes without SAS->Python mapping")

    if markdown_mapping > 0:
        out.append(
            f"Semantic audit found {markdown_mapping} mapping(s) pointing to markdown instead of code cells"
        )

    if high_count > 0:
        out.append(f"Semantic audit found {high_count} high-severity issue(s)")
        shown = 0
        for issue in issues:
            if issue.get("severity") != "high":
                continue
            node_label = issue.get("node_label", "")
            node_id = issue.get("node_id", "")
            detail = issue.get("detail", "")
            out.append(f"High issue [{node_label or node_id}]: {detail}")
            shown += 1
            if shown >= 5:
                break

    return out


def _phase7_semantic_regression_errors(state_path: Path) -> list[str]:
    """Keep phase 7 blocked while high-severity semantic issues remain."""
    audit_path = state_path / "node_translation_audit.json"
    if not audit_path.exists():
        return ["Missing artifact: node_translation_audit.json"]

    schema_errors = validate_artifact_file(audit_path, "node_translation_audit")
    if schema_errors:
        return [f"node_translation_audit.json: {e}" for e in schema_errors]

    report = json.loads(audit_path.read_text(encoding="utf-8"))
    high_count = int(report.get("summary", {}).get("issues_by_severity", {}).get("high", 0))
    issues = report.get("issues", [])
    markdown_mapping = sum(
        1
        for issue in issues
        if issue.get("category") == "traceability"
        and "points to markdown" in str(issue.get("detail", "")).lower()
    )
    if high_count > 0:
        return [f"Semantic audit still has {high_count} high-severity issue(s)"]
    if markdown_mapping > 0:
        return [
            f"Semantic audit still has {markdown_mapping} mapping(s) pointing to markdown instead of code"
        ]
    return []


def check_gate(phase: int, state_dir: str | Path) -> tuple[bool, list[str]]:
    """Check if all required artifacts for a phase gate are valid.
    
    Returns (passed, error_messages).
    """
    state_path = Path(state_dir)
    requirements = GATE_REQUIREMENTS.get(phase, [])
    all_errors: list[str] = []

    for artifact_file, schema_name in requirements:
        artifact_path = state_path / artifact_file
        if not artifact_path.exists():
            all_errors.append(f"Missing artifact: {artifact_file}")
            continue
        errors = validate_artifact_file(artifact_path, schema_name)
        for e in errors:
            all_errors.append(f"{artifact_file}: {e}")

    if phase == 2:
        all_errors.extend(_phase2_completeness_errors(state_path))

    if phase == 3:
        all_errors.extend(_phase3_errors(state_path))

    if phase == 4:
        all_errors.extend(_phase4_errors(state_path))

    if phase == 5:
        all_errors.extend(_phase5_errors(state_path))

    if phase == 6:
        all_errors.extend(_phase6_generation_errors(state_path))
        all_errors.extend(_phase6_semantic_audit_errors(state_path))

    if phase == 7:
        all_errors.extend(_phase7_semantic_regression_errors(state_path))

    if phase == 8:
        all_errors.extend(_phase8_errors(state_path))

    return (len(all_errors) == 0, all_errors)
