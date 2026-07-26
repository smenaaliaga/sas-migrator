#!/usr/bin/env python3
"""Generate JSON Schema files from all Pydantic models.

Usage:
    python generate_schemas.py [--output-dir schemas/]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sas_migrator.core.models.analysis import (
    AnalysisProgress,
    CodeSmell,
    Improvement,
    LineageEntry,
    NodeClassification,
)
from sas_migrator.core.models.artifacts import IntakeCatalog
from sas_migrator.core.models.data import (
    ColumnMapping,
    DatabaseConnection,
    DatabaseConnections,
    DataProfile,
    FileMapping,
    PreprocessCandidate,
)
from sas_migrator.core.models.graph import ExtractionResidue, FlowGraph, FlowSummary, SASNode
from sas_migrator.core.models.interview import InterviewQA, QuestionBlock
from sas_migrator.core.models.state import GateCheck, IterationEntry, MigrationState, PhaseResult
from sas_migrator.core.models.translation import TranslationPlan
from sas_migrator.core.models.validation import (
    MismatchDiagnosis,
    NodeTranslationAuditReport,
    TestResult,
    ValidationReport,
)

# Map schema_name → Pydantic model
SCHEMA_MAP: dict[str, type] = {
    "migration_state": MigrationState,
    "gate_check": GateCheck,
    "phase_result": PhaseResult,
    "flow_graph": FlowGraph,
    "flow_summary": FlowSummary,
    "sas_node": SASNode,
    "extraction_residue": ExtractionResidue,
    "node_classification": NodeClassification,
    "analysis_progress": AnalysisProgress,
    "lineage": LineageEntry,
    "code_smells": CodeSmell,
    "improvements": Improvement,
    "data_profile": DataProfile,
    "file_mapping": FileMapping,
    "column_mapping": ColumnMapping,
    "preprocess_candidate": PreprocessCandidate,
    "db_connection": DatabaseConnection,
    "db_connections": DatabaseConnections,
    "interview": InterviewQA,
    "question_block": QuestionBlock,
    "validation_report": ValidationReport,
    "test_result": TestResult,
    "mismatch_diagnosis": MismatchDiagnosis,
    "node_translation_audit": NodeTranslationAuditReport,
    "iteration_entry": IterationEntry,
    # Aggregate schemas (arrays) used by gate validation
    "profile_report": DataProfile,
    "intake": IntakeCatalog,
    "translation_plan": TranslationPlan,
}


def generate(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, model in SCHEMA_MAP.items():
        schema = model.model_json_schema()
        path = output_dir / f"{name}.schema.json"
        path.write_text(
            json.dumps(schema, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"  ✓ {path.name}")


if __name__ == "__main__":
    from sas_migrator.core.utils.schema_validation import SCHEMAS_DIR
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else SCHEMAS_DIR
    print(f"Generating JSON Schemas → {out}/")
    generate(out)
    print("Done.")
