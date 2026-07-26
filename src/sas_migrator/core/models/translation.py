"""Contratos del plan de traduccion SAS a Python."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class OutputStrategy(str, Enum):
    NOTEBOOK_FLOW = "notebook-flow"
    SINGLE = "single"
    HYBRID = "hybrid"


class TranslationTarget(BaseModel):
    """Plan de traduccion para un nodo del DAG o grupo de notebook."""

    node_id: str
    node_label: str = ""
    node_type: str = ""
    strategy: str = "pandas"
    notebook_path: str | None = None
    input_datasets: list[str] = Field(default_factory=list)
    output_datasets: list[str] = Field(default_factory=list)
    input_dir: str | None = None
    input_files: list[str] = Field(default_factory=list)
    output_files: list[str] = Field(default_factory=list)
    output_tables: list[str] = Field(default_factory=list)
    reference_csv: str | None = None
    approved_improvements: list[str] = Field(default_factory=list)
    preprocess_steps: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    notes: str = ""


class TranslationPlan(BaseModel):
    """Plan aprobado de Fase 5 consumido por el agente translator."""

    project_name: str = ""
    egp_file: str = ""
    output_strategy: OutputStrategy = OutputStrategy.NOTEBOOK_FLOW
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    targets: list[TranslationTarget] = Field(default_factory=list)
    ignored_nodes: list[str] = Field(default_factory=list)
    global_improvements: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    user_approved: bool = False