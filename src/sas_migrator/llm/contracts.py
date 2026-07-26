"""Output models de las llamadas LLM (structured outputs).

Reutilizan los modelos core donde existe contrato (Improvement, FileMapping,
NodeTranslation). Restricción de structured outputs: no hay dicts de claves
libres — ``category_scan`` es una lista que el runner convierte a dict al
escribir el artefacto.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sas_migrator.core.models.analysis import Improvement
from sas_migrator.core.models.data import FileMapping
from sas_migrator.core.models.validation import MismatchDiagnosis


class NodeReviewNote(BaseModel):
    node_id: str
    note: str  # propósito + riesgo de traducción, 1 línea, ÚNICA por nodo


class PfdAnalysisOut(BaseModel):
    """Salida del map por Process Flow (fase 2)."""

    pfd_id: str
    flow_description: str  # descripción de negocio del flujo, 1-2 frases
    reviews: list[NodeReviewNote] = Field(default_factory=list)


class CategoryVerdict(BaseModel):
    category: str
    verdict: str


class ImprovementsOut(BaseModel):
    """Salida del reduce global de mejoras (fase 2)."""

    improvements: list[Improvement] = Field(default_factory=list)
    category_scan: list[CategoryVerdict] = Field(default_factory=list)


class FileMappingBatch(BaseModel):
    """Salida del matching archivo↔nodo (fase 3)."""

    mappings: list[FileMapping] = Field(default_factory=list)


class DiagnosesOut(BaseModel):
    """Salida del diagnóstico de mismatches (fase 7) — reusa MismatchDiagnosis
    tipado con los 8 patrones de causa (MismatchCause)."""

    diagnoses: list[MismatchDiagnosis] = Field(default_factory=list)


class DocsOut(BaseModel):
    """Salida del doc-writer (fase 8) — el único nodo donde la prosa es el
    producto. Cinco documentos markdown en español."""

    readme: str
    lineage: str
    decisions: str
    improvements: str
    runbook: str
