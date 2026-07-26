import json

import pytest
import yaml

from sas_migrator.core.utils import schema_validation


def test_validate_artifact_supports_wrapped_improvements(tmp_path, monkeypatch) -> None:
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    (schema_dir / "improvements.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["id", "category", "title"],
                "properties": {
                    "id": {"type": "string"},
                    "category": {"type": "string"},
                    "title": {"type": "string"},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(schema_validation, "SCHEMAS_DIR", schema_dir)

    errors = schema_validation.validate_artifact(
        {"improvements": [{"id": "M-001", "category": "performance", "title": "Optimizar"}]},
        "improvements",
    )

    assert errors == []


def test_load_schema_reports_missing_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(schema_validation, "SCHEMAS_DIR", tmp_path)

    with pytest.raises(FileNotFoundError):
        schema_validation.load_schema("missing")


def test_phase6_semantic_gate_blocks_high_issues(tmp_path, monkeypatch) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    monkeypatch.setattr(schema_validation, "_phase6_generation_errors", lambda _: [])
    monkeypatch.setattr(
        schema_validation,
        "_phase6_semantic_audit_errors",
        lambda _: ["Semantic audit found 2 high-severity issue(s)"],
    )

    passed, errors = schema_validation.check_gate(6, state_dir)

    assert not passed
    assert errors == ["Semantic audit found 2 high-severity issue(s)"]


def test_phase7_semantic_check_is_enforced(tmp_path, monkeypatch) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "validation_report.json").write_text("{}", encoding="utf-8")
    (state_dir / "node_translation_audit.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(schema_validation, "validate_artifact_file", lambda *_: [])
    monkeypatch.setattr(
        schema_validation,
        "_phase7_semantic_regression_errors",
        lambda _: ["Semantic audit still has 1 high-severity issue(s)"],
    )

    passed, errors = schema_validation.check_gate(7, state_dir)

    assert not passed
    assert "Semantic audit still has 1 high-severity issue(s)" in errors


def test_phase6_semantic_gate_blocks_markdown_mapping(monkeypatch, tmp_path) -> None:
    report = {
        "summary": {
            "issues_by_severity": {"high": 0, "medium": 0, "low": 1},
            "missing_mapping_count": 0,
        },
        "issues": [
            {
                "category": "traceability",
                "detail": "cell_index=4 points to markdown; effective code appears at cell_index=5",
            }
        ],
    }

    monkeypatch.setattr(schema_validation, "_run_node_translation_audit", lambda _: (report, []))

    errors = schema_validation._phase6_semantic_audit_errors(tmp_path)

    assert errors == [
        "Semantic audit found 1 mapping(s) pointing to markdown instead of code cells"
    ]


def test_validate_artifact_checks_items_beyond_the_fifth(tmp_path, monkeypatch) -> None:
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    (schema_dir / "lineage.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["source_node"],
                "properties": {"source_node": {"type": "string"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(schema_validation, "SCHEMAS_DIR", schema_dir)

    data = [{"source_node": f"n{i}"} for i in range(6)] + [{"bad": True}]
    errors = schema_validation.validate_artifact(data, "lineage")

    assert errors, "el item 7 inválido debe detectarse (antes solo se validaban los primeros 5)"
    assert "[item 6]" in errors[0]


def _phase2_state(
    tmp_path,
    *,
    unexplained=0,
    pending=0,
    classification="transform",
    description="Carga y consolida ventas mensuales.",
    review_notes=("Lee VENTAS desde ODBC y agrega por mes; sin riesgos.",),
    scan_summary=None,
    smells=None,
):
    state = tmp_path / "state"
    (state / "nodes").mkdir(parents=True)
    node = {"id": "CodeTask-1", "metadata": {}}
    if classification:
        node["metadata"]["classification"] = classification
    (state / "nodes" / "CodeTask-1.json").write_text(
        json.dumps(node), encoding="utf-8"
    )
    (state / "flow_graph.json").write_text(
        json.dumps({"nodes": [{"id": "CodeTask-1"}]}), encoding="utf-8"
    )
    (state / "flow_summary.json").write_text(
        json.dumps(
            {
                "migratable_flows": 1,
                "flows": [
                    {
                        "pfd_id": "PFD-1",
                        "migratable_candidate": True,
                        "description": description,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (state / "extraction_residue.json").write_text(
        json.dumps({"unexplained_count": unexplained}), encoding="utf-8"
    )
    entries = [{"node_id": "CodeTask-1", "status": "reviewed"}]
    entries += [{"node_id": f"P{i}", "status": "pending"} for i in range(pending)]
    (state / "analysis_progress.json").write_text(
        json.dumps({"nodes": entries}), encoding="utf-8"
    )

    reviews_dir = state / "analysis_reviews"
    reviews_dir.mkdir()
    (reviews_dir / "PFD-1.json").write_text(
        json.dumps(
            {
                "pfd_id": "PFD-1",
                "reviews": [
                    {"node_id": f"CodeTask-{i}", "note": note}
                    for i, note in enumerate(review_notes)
                ],
            }
        ),
        encoding="utf-8",
    )

    (state / "code_smells.json").write_text(
        json.dumps({"smells": smells if smells is not None else []}), encoding="utf-8"
    )
    (state / "improvements_proposed.yaml").write_text(
        yaml.safe_dump(
            {
                "improvements": [],
                "category_scan_summary": scan_summary
                if scan_summary is not None
                else {"performance": "No improvements found"},
            }
        ),
        encoding="utf-8",
    )
    return state


def test_phase2_clean_state_passes(tmp_path) -> None:
    state = _phase2_state(tmp_path)
    assert schema_validation._phase2_completeness_errors(state) == []


def test_phase2_blocks_on_unexplained_residue(tmp_path) -> None:
    state = _phase2_state(tmp_path, unexplained=3)
    errors = schema_validation._phase2_completeness_errors(state)
    assert any("unexplained" in e for e in errors)


def test_phase2_blocks_on_pending_analysis(tmp_path) -> None:
    state = _phase2_state(tmp_path, pending=2)
    errors = schema_validation._phase2_completeness_errors(state)
    assert any("pending review" in e for e in errors)


# ── Sustancia: el gate no debe pasar sobre un análisis vacío ────────────────

def test_phase2_blocks_on_unclassified_nodes(tmp_path) -> None:
    """Una re-extracción posterior al análisis borra metadata.classification."""
    state = _phase2_state(tmp_path, classification=None)
    errors = schema_validation._phase2_completeness_errors(state)
    assert any("classification" in e for e in errors)


def test_phase2_blocks_on_flows_without_description(tmp_path) -> None:
    state = _phase2_state(tmp_path, description="")
    errors = schema_validation._phase2_completeness_errors(state)
    assert any("description" in e for e in errors)


def test_phase2_blocks_on_boilerplate_reviews(tmp_path) -> None:
    """84 notas idénticas no son 84 revisiones."""
    state = _phase2_state(tmp_path, review_notes=("Revisado en análisis determinista.",) * 6)
    errors = schema_validation._phase2_completeness_errors(state)
    assert any("plantilla repetida" in e for e in errors)


def test_phase2_accepts_distinct_reviews(tmp_path) -> None:
    state = _phase2_state(
        tmp_path,
        review_notes=(
            "Lee VENTAS desde ODBC; join con calendario.",
            "Agrega por sucursal; CASE WHEN con 12 ramas — revisar redondeo.",
            "Exporta a Excel con formato; sin lógica de negocio.",
        ),
    )
    assert schema_validation._phase2_completeness_errors(state) == []


def test_phase2_blocks_when_scan_summary_contradicts_smells(tmp_path) -> None:
    state = _phase2_state(
        tmp_path,
        scan_summary={"quality": "No improvements found"},
        smells=[{"id": "CS-1", "category": "quality"}, {"id": "CS-2", "category": "quality"}],
    )
    errors = schema_validation._phase2_completeness_errors(state)
    assert any("category_scan_summary" in e for e in errors)


def test_phase2_allows_empty_category_without_evidence(tmp_path) -> None:
    state = _phase2_state(
        tmp_path,
        scan_summary={"security": "No improvements found"},
        smells=[{"id": "CS-1", "category": "quality"}],
    )
    assert schema_validation._phase2_completeness_errors(state) == []


def test_phase2_blocks_on_node_file_mismatch(tmp_path) -> None:
    state = _phase2_state(tmp_path)
    (state / "flow_graph.json").write_text(
        json.dumps({"nodes": [{"id": "CodeTask-1"}, {"id": "CodeTask-2"}]}),
        encoding="utf-8",
    )
    errors = schema_validation._phase2_completeness_errors(state)
    assert any("do not match" in e for e in errors)


def test_phase3_requires_column_mapping_when_profiles_exist(tmp_path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "profile_report.json").write_text(
        json.dumps([{"file": "datos.csv"}]), encoding="utf-8"
    )
    errors = schema_validation._phase3_errors(state)
    assert any("column_mapping.yaml" in e for e in errors)

    (state / "column_mapping.yaml").write_text("mappings: []\n", encoding="utf-8")
    assert schema_validation._phase3_errors(state) == []


def test_phase3_escape_without_profiled_files(tmp_path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "profile_report.json").write_text("[]", encoding="utf-8")
    assert schema_validation._phase3_errors(state) == []


def test_phase4_requires_ignored_nodes_and_decisions(tmp_path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "improvements_proposed.yaml").write_text(
        "improvements:\n  - id: M-001\n    category: performance\n", encoding="utf-8"
    )
    (state / "approved_improvements.yaml").write_text("improvements: []\n", encoding="utf-8")

    errors = schema_validation._phase4_errors(state)
    assert any("ignored_nodes.yaml" in e for e in errors)
    assert any("M-001" in e for e in errors)

    (state / "ignored_nodes.yaml").write_text("ignored_nodes: []\n", encoding="utf-8")
    (state / "approved_improvements.yaml").write_text(
        "improvements:\n  - id: M-001\n    status: postponed\n", encoding="utf-8"
    )
    assert schema_validation._phase4_errors(state) == []


def _phase5_state(tmp_path, *, plan_nodes, approved=True, ignored=()):
    state = tmp_path / "state"
    state.mkdir()
    (state / "nodes_index.json").write_text(
        json.dumps({"nodes": [{"id": "CT-1"}, {"id": "CT-2"}, {"id": "CT-3"}]}),
        encoding="utf-8",
    )
    if ignored:
        (state / "ignored_nodes.yaml").write_text(
            "ignored_nodes:\n" + "".join(f"  - {n}\n" for n in ignored), encoding="utf-8"
        )
    (state / "translation_plan.json").write_text(
        json.dumps(
            {
                "user_approved": approved,
                "ignored_nodes": list(ignored),
                "targets": [{"node_id": n} for n in plan_nodes],
            }
        ),
        encoding="utf-8",
    )
    return state


def test_phase5_full_coverage_passes(tmp_path) -> None:
    state = _phase5_state(tmp_path, plan_nodes=["CT-1", "CT-2"], ignored=["CT-3"])
    assert schema_validation._phase5_errors(state) == []


def test_phase5_blocks_on_missing_node(tmp_path) -> None:
    state = _phase5_state(tmp_path, plan_nodes=["CT-1"], ignored=["CT-3"])
    errors = schema_validation._phase5_errors(state)
    assert any("does not cover" in e and "CT-2" in e for e in errors)


def test_phase5_blocks_without_user_approval(tmp_path) -> None:
    state = _phase5_state(
        tmp_path, plan_nodes=["CT-1", "CT-2", "CT-3"], approved=False
    )
    errors = schema_validation._phase5_errors(state)
    assert any("user_approved" in e for e in errors)


def test_phase8_requires_all_docs(tmp_path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    docs = tmp_path / "output" / "docs"
    docs.mkdir(parents=True)
    for name in ("README.md", "LINEAGE.md", "DECISIONS.md", "IMPROVEMENTS.md"):
        (docs / name).write_text("# contenido\n", encoding="utf-8")

    errors = schema_validation._phase8_errors(state)
    assert errors == ["Missing doc: output/docs/RUNBOOK.md"]

    (docs / "RUNBOOK.md").write_text("# runbook\n", encoding="utf-8")
    assert schema_validation._phase8_errors(state) == []


def test_phase7_semantic_check_blocks_markdown_mapping(tmp_path, monkeypatch) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "node_translation_audit.json").write_text(
        json.dumps(
            {
                "summary": {
                    "issues_by_severity": {"high": 0, "medium": 0, "low": 1},
                },
                "issues": [
                    {
                        "category": "traceability",
                        "detail": "cell_index=4 points to markdown; effective code appears at cell_index=5",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(schema_validation, "validate_artifact_file", lambda *_: [])

    errors = schema_validation._phase7_semantic_regression_errors(state_dir)

    assert errors == [
        "Semantic audit still has 1 mapping(s) pointing to markdown instead of code"
    ]