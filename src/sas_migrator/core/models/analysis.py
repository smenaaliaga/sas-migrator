"""Analysis models — classification, lineage, code smells, improvements."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

# ── Analysis progress ledger ────────────────────────────────────────────────

class NodeReview(BaseModel):
    """Estado de revisión de un nodo en el ledger nodo-por-nodo de Fase 2."""

    node_id: str
    batch: str = ""  # pfd_id del Process Flow (lote de invocación)
    status: str = "pending"  # pending | reviewed
    note: str = ""  # 1 línea del code-analyst: propósito + riesgo de traducción
    reviewed_at: datetime | None = None


class AnalysisProgress(BaseModel):
    """Ledger de cobertura del análisis LLM — ``state/analysis_progress.json``.

    Garantiza en disco lo que el contexto del agente no puede: que CADA nodo
    fue leído por code-analyst. El gate 2 exige cero ``pending``.
    """

    generated_at: datetime = Field(default_factory=datetime.utcnow)
    total_nodes: int = 0
    reviewed: int = 0
    pending: int = 0
    nodes: list[NodeReview] = Field(default_factory=list)


# ── Classification ──────────────────────────────────────────────────────────

class NodeClassification(BaseModel):
    """Classification result for a single SAS node."""

    node_id: str
    node_type: str  # matches NodeType values
    sub_type: str = ""  # e.g. "PROC REPORT", "DATA step with MERGE"
    summary: str = ""  # one-line description of what the node does
    complexity: str = "low"  # low | medium | high


# ── Lineage ─────────────────────────────────────────────────────────────────

class LineageEntry(BaseModel):
    """Column-level lineage: how a column flows through a node."""

    node_id: str
    source_dataset: str
    source_column: str
    target_dataset: str
    target_column: str
    transformation: str = ""  # description of what happens (rename, calc, pass-through)


# ── Code Smells ─────────────────────────────────────────────────────────────

class SmellSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class CodeSmell(BaseModel):
    """A problematic pattern detected in SAS code."""

    id: str  # CS-001, CS-002, ...
    node_id: str
    category: str  # dynamic_macro, float_comparison, disconnected_node, ...
    description: str
    severity: SmellSeverity = SmellSeverity.WARNING
    line_hint: str = ""  # approximate location in the code
    recommendation: str = ""


# ── Improvements ────────────────────────────────────────────────────────────

class ImprovementCategory(str, Enum):
    PERFORMANCE = "performance"
    QUALITY = "quality"
    MAINTAINABILITY = "maintainability"
    REPRODUCIBILITY = "reproducibility"
    MODERNIZATION = "modernization"
    SECURITY = "security"
    OBSERVABILITY = "observability"
    ARCHITECTURE = "architecture"


class ImprovementStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    POSTPONED = "postponed"


class Improvement(BaseModel):
    """A proposed improvement — presented as a card (ficha) to the user."""

    id: str  # M-001, M-002, ...
    category: ImprovementCategory
    title: str
    description: str
    justification: str
    impact: str  # low | medium | high
    effort: str  # low | medium | high
    risk: str    # low | medium | high
    recommendation: str
    affected_nodes: list[str] = Field(default_factory=list)
    status: ImprovementStatus = ImprovementStatus.PROPOSED
    user_comment: str = ""
