"""Validation models — test results, mismatch diagnosis, validation report."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TestType(str, Enum):
    SCHEMA = "schema"
    ROW_COUNT = "row_count"
    AGGREGATES = "aggregates"
    ROW_BY_ROW = "row_by_row"


class TestStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class TestResult(BaseModel):
    """Result of a single validation test."""

    test_type: TestType
    status: TestStatus
    dataset: str = ""
    details: str = ""
    expected: str | None = None
    actual: str | None = None
    tolerance_used: float | None = None


class MismatchCause(str, Enum):
    ENCODING = "encoding"
    ROUNDING = "rounding"
    NULL_SEMANTICS = "null_semantics"
    COLLATION = "collation"
    ROW_ORDER = "row_order"
    TYPE_COERCION = "type_coercion"
    MISSING_TRANSFORM = "missing_transform"
    UNKNOWN = "unknown"


class MismatchDiagnosis(BaseModel):
    """Diagnosis of a test failure."""

    test_type: TestType
    dataset: str
    column: str = ""
    expected_value: str = ""
    actual_value: str = ""
    probable_cause: MismatchCause = MismatchCause.UNKNOWN
    explanation: str = ""
    proposed_fix: str = ""


class ValidationReport(BaseModel):
    """Complete validation report for a migration run."""

    model_config = ConfigDict(extra="allow")

    validation_mode: str = "not_applicable"
    tests: list[TestResult] = Field(default_factory=list)
    diagnoses: list[MismatchDiagnosis] = Field(default_factory=list)
    overall_passed: bool | None = None
    user_accepted_deviations: list[str] = Field(default_factory=list)
    total_files: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    pass_rate_value: float | None = Field(default=None, alias="pass_rate")
    unmatched_reference: list[str] = Field(default_factory=list)
    unmatched_generated: list[str] = Field(default_factory=list)
    results: list[dict[str, Any]] = Field(default_factory=list)
    notebooks: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    note: str = ""
    run_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def pass_rate(self) -> float:
        if self.pass_rate_value is not None:
            return self.pass_rate_value
        if not self.tests:
            return 0.0
        passed = sum(1 for t in self.tests if t.status == TestStatus.PASSED)
        return passed / len(self.tests)


class NodeTranslationSeverity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class NodeTranslationIssueCategory(str, Enum):
    COVERAGE = "coverage"
    TRACEABILITY = "traceability"
    SEMANTIC = "semantic"
    SECURITY = "security"
    RUNTIME = "runtime"


class NodeTranslationAuditIssue(BaseModel):
    """Single issue found by the SAS->Python node translation audit."""

    severity: NodeTranslationSeverity
    category: NodeTranslationIssueCategory
    node_id: str
    node_label: str = ""
    notebook_path: str = ""
    detail: str


class NodeTranslationSeveritySummary(BaseModel):
    """Count of issues by severity."""

    high: int = 0
    medium: int = 0
    low: int = 0


class NodeTranslationAuditSummary(BaseModel):
    """Summary section for node translation audit output."""

    nodes_total: int = 0
    nodes_in_scope: int = 0
    ignored_count: int = 0
    mappings_total: int = 0
    missing_mapping_count: int = 0
    extra_mapping_count: int = 0
    issues_total: int = 0
    issues_by_severity: NodeTranslationSeveritySummary = Field(
        default_factory=NodeTranslationSeveritySummary
    )


class NodeTranslationAuditReport(BaseModel):
    """Complete node translation semantic audit report."""

    generated_at: datetime = Field(default_factory=datetime.utcnow)
    summary: NodeTranslationAuditSummary
    issues: list[NodeTranslationAuditIssue] = Field(default_factory=list)
