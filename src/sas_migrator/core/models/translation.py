"""Contratos del plan de traduccion SAS a Python."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

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
    # Variables macro que el nodo usa (&ANIO): suben a la celda de parámetros
    # del notebook. Sin este campo en el modelo, validar el plan las borraba.
    macro_params: list[str] = Field(default_factory=list)
    notes: str = ""


class TranslationPlan(BaseModel):
    """Plan aprobado de Fase 5 consumido por el agente translator."""

    project_name: str = ""
    egp_file: str = ""
    output_strategy: OutputStrategy = OutputStrategy.NOTEBOOK_FLOW
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    targets: list[TranslationTarget] = Field(default_factory=list)
    # Valores de las macro vars (project_config.yaml → run.macro_params); el
    # ensamblador escribe la celda de parámetros desde acá sin releer config.
    macro_param_values: dict[str, Any] = Field(default_factory=dict)
    # Decisión B5b de la entrevista: logging liviano por celda (`_log` +
    # output/log/<notebook>.log). Traductor y ensamblador la leen de acá.
    cell_logging: bool = False
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


# Las 5 strategies válidas — las mismas que STRATEGY_BY_PLACEMENT produce.
TranslationStrategy = Literal["pandas", "python", "sql_passthrough", "sql_pushdown", "hybrid"]


class NodeTranslation(BaseModel):
    """Output estructurado del traductor para UN nodo.

    Es también el ``output_model`` de la llamada LLM (structured output): las
    ``description`` de los campos viajan en el JSON Schema al modelo, así que
    son instrucciones, no decoración. Contrato FUERTE a propósito — en
    producción ``cells: []`` validó dos veces y produjo nodos vacíos que
    parecían traducidos; con ``min_length=1`` eso es un error de validación
    con retry inmediato, no un artefacto persistido.

    Las ``cells`` van SIN header ni ancla — los agrega el ensamblador, que es
    quien calcula ``cell_index`` por construcción.
    """

    node_id: str = Field(description="Copia EXACTA del node_id del mensaje user.")
    node_label: str = Field(default="", description="Copia del node_label del user.")
    strategy: TranslationStrategy = Field(
        default="pandas",
        description="Copia de la strategy del plan para este nodo — no se renegocia acá.",
    )
    imports: list[str] = Field(
        default_factory=list,
        description=(
            "Líneas de import completas ('import pandas as pd'). Solo stdlib y "
            "librerías permitidas del proyecto. Nunca imports dentro de cells."
        ),
    )
    cells: list[str] = Field(
        min_length=1,
        description=(
            "1..n celdas de código Python SIN headers ni anclas. Un nodo sin "
            "código traducible NO devuelve cells vacías: devuelve una celda con "
            "raise NotImplementedError('<qué falta>') y el motivo en warnings."
        ),
    )
    traceability: Traceability = Field(
        default_factory=Traceability,
        description="Construct SAS dominante y regla de negocio en 1 línea.",
    )
    confidence: Confidence = Field(
        default=Confidence.LOW,
        description="Confianza honesta: low si hubo supuestos fuertes.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Supuestos tomados, ambigüedades, todo lo que un revisor debe mirar.",
    )


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