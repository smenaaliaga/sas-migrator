"""Output models de las llamadas LLM (structured outputs).

Reutilizan los modelos core donde existe contrato (Improvement, FileMapping,
NodeTranslation). Restricción de structured outputs: no hay dicts de claves
libres — ``category_scan`` es una lista que el runner convierte a dict al
escribir el artefacto.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from sas_migrator.core.models.analysis import Improvement
from sas_migrator.core.models.data import FileMapping
from sas_migrator.core.models.translation import Confidence
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


class VerifyIssue(BaseModel):
    """Un problema concreto que el verificador encontró en la traducción."""

    kind: Literal[
        "missing_logic",      # una parte del SAS no aparece en el Python
        "wrong_semantics",    # el Python hace OTRA cosa (filtro, join, agregación)
        "wrong_write",        # la semántica de escritura a BD no replica al SAS
        "wrong_names",        # datasets/columnas con nombre equivocado
        "other",
    ] = Field(description="Tipo del problema.")
    detail: str = Field(
        description="Qué está mal y dónde, citando el SAS y el Python relevantes."
    )


class TranslationVerdict(BaseModel):
    """Salida del verificador independiente de traducciones (fase 6).

    Es el segundo par de ojos: compara el SAS original contra la traducción
    que YA pasó los chequeos estáticos, buscando lo que ellos no ven —
    semántica perdida o cambiada.
    """

    node_id: str = Field(description="Copia exacta del node_id del user.")
    verdict: Literal["approve", "revise"] = Field(
        description=(
            "'revise' SOLO por problemas sustantivos de semántica (la cifra "
            "saldría distinta). Estilo, nombres de variables internos o "
            "equivalencias válidas NO ameritan revise."
        )
    )
    issues: list[VerifyIssue] = Field(
        default_factory=list,
        description="Vacía si approve; con revise, los problemas concretos.",
    )
    confidence: Confidence = Field(
        default=Confidence.MEDIUM,
        description="Confianza del verificador en que la traducción es correcta.",
    )


class DocsOut(BaseModel):
    """Salida del doc-writer (fase 8) — el único nodo donde la prosa es el
    producto. Cinco documentos markdown en español."""

    readme: str
    lineage: str
    decisions: str
    improvements: str
    runbook: str
