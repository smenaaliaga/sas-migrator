"""Pydantic models — typed contracts for all pipeline artifacts."""

from sas_migrator.core.models.analysis import (
    CodeSmell,
    Improvement,
    ImprovementCategory,
    ImprovementStatus,
    LineageEntry,
    SmellSeverity,
)
from sas_migrator.core.models.artifacts import FileCatalogEntry, IntakeCatalog
from sas_migrator.core.models.data import (
    DatabaseConnection,
    DatabaseConnections,
    DataProfile,
    FileMapping,
    HumanArtifact,
)
from sas_migrator.core.models.graph import Edge, FlowGraph, SASNode
from sas_migrator.core.models.interview import Answer, InterviewQA, Question, QuestionBlock
from sas_migrator.core.models.state import GateCheck, MigrationState, PhaseResult
from sas_migrator.core.models.translation import OutputStrategy, TranslationPlan, TranslationTarget
from sas_migrator.core.models.validation import (
    MismatchDiagnosis,
    NodeTranslationAuditIssue,
    NodeTranslationAuditReport,
    NodeTranslationAuditSummary,
    TestResult,
    ValidationReport,
)

__all__ = [
    "MigrationState", "PhaseResult", "GateCheck",
    "FlowGraph", "SASNode", "Edge",
    "FileCatalogEntry", "IntakeCatalog",
    "OutputStrategy", "TranslationPlan", "TranslationTarget",
    "LineageEntry", "CodeSmell", "Improvement",
    "ImprovementStatus", "ImprovementCategory", "SmellSeverity",
    "DataProfile", "FileMapping", "HumanArtifact",
    "DatabaseConnection", "DatabaseConnections",
    "InterviewQA", "QuestionBlock", "Question", "Answer",
    "ValidationReport", "TestResult", "MismatchDiagnosis",
    "NodeTranslationAuditIssue", "NodeTranslationAuditSummary", "NodeTranslationAuditReport",
]
