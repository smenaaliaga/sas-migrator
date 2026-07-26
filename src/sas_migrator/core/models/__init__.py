"""Pydantic models — typed contracts for all pipeline artifacts."""

from sas_migrator.core.models.state import MigrationState, PhaseResult, GateCheck
from sas_migrator.core.models.graph import FlowGraph, SASNode, Edge
from sas_migrator.core.models.artifacts import FileCatalogEntry, IntakeCatalog
from sas_migrator.core.models.translation import OutputStrategy, TranslationPlan, TranslationTarget
from sas_migrator.core.models.analysis import (
    NodeClassification,
    LineageEntry,
    CodeSmell,
    Improvement,
    ImprovementStatus,
    ImprovementCategory,
    SmellSeverity,
)
from sas_migrator.core.models.data import (
    DataProfile,
    FileMapping,
    ColumnMapping,
    PreprocessCandidate,
    HumanArtifact,
    DatabaseConnection,
    DatabaseConnections,
)
from sas_migrator.core.models.interview import InterviewQA, QuestionBlock, Question, Answer
from sas_migrator.core.models.validation import (
    ValidationReport,
    TestResult,
    MismatchDiagnosis,
    NodeTranslationAuditIssue,
    NodeTranslationAuditSummary,
    NodeTranslationAuditReport,
)

__all__ = [
    "MigrationState", "PhaseResult", "GateCheck",
    "FlowGraph", "SASNode", "Edge",
    "FileCatalogEntry", "IntakeCatalog",
    "OutputStrategy", "TranslationPlan", "TranslationTarget",
    "NodeClassification", "LineageEntry", "CodeSmell", "Improvement",
    "ImprovementStatus", "ImprovementCategory", "SmellSeverity",
    "DataProfile", "FileMapping", "ColumnMapping", "PreprocessCandidate", "HumanArtifact",
    "DatabaseConnection", "DatabaseConnections",
    "InterviewQA", "QuestionBlock", "Question", "Answer",
    "ValidationReport", "TestResult", "MismatchDiagnosis",
    "NodeTranslationAuditIssue", "NodeTranslationAuditSummary", "NodeTranslationAuditReport",
]
