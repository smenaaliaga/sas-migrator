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
    # Placement efectivo (clasificador + overrides de la entrevista B4b).
    placement: str | None = None
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


# ── Output estructurado de la traducción (Etapa 4) ──────────────────────────

class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Traceability(BaseModel):
    """Trazabilidad SAS→Python de un nodo traducido."""

    sas_construct: str = ""  # "PROC SQL CREATE TABLE", "DATA step MERGE", ...
    business_rule: str = ""  # 1 línea, lenguaje de negocio


class NodeTranslation(BaseModel):
    """Output estructurado del traductor para UN nodo.

    Es también el ``output_model`` de la llamada LLM (structured output). Las
    ``cells`` van SIN header ni ancla — los agrega el ensamblador, que es
    quien calcula ``cell_index`` por construcción.
    """

    node_id: str
    node_label: str = ""
    strategy: str = "pandas"
    imports: list[str] = Field(default_factory=list)  # líneas completas: "import pandas as pd"
    cells: list[str] = Field(default_factory=list)  # ≥1 celda de código
    traceability: Traceability = Field(default_factory=Traceability)
    confidence: Confidence = Confidence.LOW
    warnings: list[str] = Field(default_factory=list)


class MappingEntry(BaseModel):
    """Una fila de sas_python_mapping.json — la escribe el ensamblador."""

    node_id: str
    node_label: str = ""
    sas_construct: str = ""
    python_artifact: str = ""  # == notebook_path (compat v1)
    notebook_path: str = ""  # canónico: relativo al workspace, "output/NB-01_x.ipynb"
    cell_index: int = 0  # PRIMERA celda code del nodo, calculada al ensamblar
    cell_count: int = 1  # nº de celdas code del nodo
    business_rule: str = ""
    confidence: Confidence = Confidence.LOW


class SasPythonMapping(BaseModel):
    """Artefacto state/sas_python_mapping.json — correcto por construcción."""

    mappings: list[MappingEntry] = Field(default_factory=list)