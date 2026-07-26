"""State models — MigrationState, PhaseResult, GateCheck."""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum

from pydantic import BaseModel, Field


class Phase(IntEnum):
    INTAKE = 0
    INITIAL_INTERVIEW = 1
    SAS_ANALYSIS = 2
    PROFILING_MATCHING = 3
    POST_ANALYSIS_INTERVIEW = 4
    TRANSLATION_PLAN = 5
    GENERATION = 6
    VALIDATION = 7
    DOCUMENTATION = 8
    POST_MIGRATION = 9


class GateCheck(BaseModel):
    """Result of a gate validation between phases."""

    phase: Phase
    passed: bool
    errors: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=datetime.utcnow)


class PhaseResult(BaseModel):
    """Outcome of executing a single phase."""

    phase: Phase
    started_at: datetime
    completed_at: datetime | None = None
    artifacts_produced: list[str] = Field(default_factory=list)
    gate: GateCheck | None = None


class Decision(BaseModel):
    """A decision made during the migration."""

    id: str
    description: str
    decided_at: datetime = Field(default_factory=datetime.utcnow)
    context: str = ""


class OpenQuestion(BaseModel):
    """A question pending resolution."""

    id: str
    question: str
    source_agent: str
    phase: Phase
    resolved: bool = False
    answer: str | None = None


class IterationEntry(BaseModel):
    """A single post-migration iteration request and its outcome."""

    id: str  # IT-001, IT-002, ...
    cycle: int  # iteration cycle number (1, 2, 3, ...)
    request_type: str  # "bug_fix" | "enhancement" | "postponed_improvement" | "new_requirement" | "data_change" | "context_update"
    description: str
    affected_notebooks: list[str] = Field(default_factory=list)
    affected_nodes: list[str] = Field(default_factory=list)
    related_improvement: str | None = None  # M-xxx if applies
    context_questions: list[str] = Field(default_factory=list)  # questions asked to user
    context_answers: list[str] = Field(default_factory=list)  # user answers
    changes_made: list[str] = Field(default_factory=list)  # files modified
    validation_result: str | None = None  # "PASS" | "WARN" | "FAIL" | None
    status: str = "open"  # "open" | "in_progress" | "completed" | "deferred"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None


class MigrationState(BaseModel):
    """Root state object — persisted as state/migration_state.json."""

    project_name: str = ""
    egp_file: str | None = None
    framework_mode: str = "generic"
    current_phase: Phase = Phase.INTAKE
    output_strategy: str | None = "notebook-flow"  # "notebook-flow" | "single" | "hybrid"
    phases_completed: list[PhaseResult] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)  # artifact_name → file path
    decisions: list[Decision] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    improvements_proposed: int = 0
    improvements_approved: int = 0
    improvements_rejected: int = 0
    improvements_postponed: int = 0
    iteration_cycle: int = 0  # current post-migration iteration cycle
    iteration_log: list[IterationEntry] = Field(default_factory=list)
    tokens_consumed: dict[str, int] = Field(default_factory=dict)  # phase → tokens
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
