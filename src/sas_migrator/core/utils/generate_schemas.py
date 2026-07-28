#!/usr/bin/env python3
"""Generate JSON Schema files from all Pydantic models.

Usage:
    python generate_schemas.py [--output-dir schemas/]

Solo se generan los schemas que `check_gate` consulta (GATE_REQUIREMENTS):
los gates validan artefactos que pudieron ser editados a mano. Todo lo demás
lo escribe y relee el runtime vía Pydantic — un JSON Schema aparte sería una
segunda copia del mismo contrato sin consumidor.
"""

from __future__ import annotations

import sys
from pathlib import Path

from sas_migrator.core.models.analysis import (
    CodeSmell,
    Improvement,
    LineageEntry,
)
from sas_migrator.core.models.artifacts import IntakeCatalog
from sas_migrator.core.models.data import (
    DataProfile,
    FileMapping,
)
from sas_migrator.core.models.graph import ExtractionResidue, FlowGraph, FlowSummary
from sas_migrator.core.models.interview import InterviewQA
from sas_migrator.core.models.translation import (
    SasPythonMapping,
    TranslationPlan,
)
from sas_migrator.core.models.validation import (
    NodeTranslationAuditReport,
    ValidationReport,
)
from sas_migrator.core.utils.fsio import dump_json

# Map schema_name → Pydantic model (1:1 con los nombres de GATE_REQUIREMENTS)
SCHEMA_MAP: dict[str, type] = {
    "intake": IntakeCatalog,
    "interview": InterviewQA,
    "flow_graph": FlowGraph,
    "flow_summary": FlowSummary,
    "extraction_residue": ExtractionResidue,
    "lineage": LineageEntry,
    "code_smells": CodeSmell,
    "improvements": Improvement,
    "profile_report": DataProfile,
    "file_mapping": FileMapping,
    "translation_plan": TranslationPlan,
    "sas_python_mapping": SasPythonMapping,
    "validation_report": ValidationReport,
    "node_translation_audit": NodeTranslationAuditReport,
}


def generate(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, model in SCHEMA_MAP.items():
        schema = model.model_json_schema()
        path = output_dir / f"{name}.schema.json"
        dump_json(path, schema, trailing_newline=True)
        print(f"  ✓ {path.name}")


if __name__ == "__main__":
    from sas_migrator.core.utils.schema_validation import SCHEMAS_DIR
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else SCHEMAS_DIR
    print(f"Generating JSON Schemas → {out}/")
    generate(out)
    print("Done.")
