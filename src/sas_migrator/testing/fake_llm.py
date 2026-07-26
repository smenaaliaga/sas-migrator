"""FakeCaller por defecto para tests y golden runs — respuestas deterministas
que satisfacen los gates de sustancia de las fases 2/3/6.

Cada respuesta parsea la primera línea del mensaje user (el header JSON que
emite ``llm.prompt_builder``) para ecoar los ids exactos.
"""

from __future__ import annotations

import json

from sas_migrator.core.models.analysis import Improvement
from sas_migrator.core.models.translation import NodeTranslation, Traceability
from sas_migrator.llm.contracts import (
    CategoryVerdict,
    FileMappingBatch,
    ImprovementsOut,
    NodeReviewNote,
    PfdAnalysisOut,
)
from sas_migrator.llm.fake import FakeCaller


def _header(user: str) -> dict:
    return json.loads(user.splitlines()[0])


def fake_reviews(user: str) -> PfdAnalysisOut:
    head = _header(user)
    return PfdAnalysisOut(
        pfd_id=head["pfd_id"],
        flow_description="Consolida ventas regionales para reportes mensuales.",
        reviews=[
            NodeReviewNote(
                node_id=nid,
                note=f"{nid}: transforma ventas del flujo; riesgo bajo de traducción.",
            )
            for nid in head["node_ids"]
        ],
    )


def fake_improvements(user: str) -> ImprovementsOut:
    head = _header(user)
    return ImprovementsOut(
        improvements=[
            Improvement(
                id="M-001", category="quality",
                title="Explicitar columnas en SELECT",
                description="Query-1 usa SELECT *.",
                justification="code_smells.json: select_star en Query-1.",
                impact="low", effort="low", risk="low",
                recommendation="approve", affected_nodes=["Query-1"],
            )
        ],
        category_scan=[
            CategoryVerdict(category=c, verdict=f"1 ficha propuesta para {c}")
            for c in head["smell_categories"]
        ],
    )


def fake_translation(user: str) -> NodeTranslation:
    head = _header(user)
    return NodeTranslation(
        node_id=head["node_id"],
        node_label=head["node_label"],
        strategy=head["strategy"],
        cells=[
            "df = pd.DataFrame()\n"
            "resultado = df.sort_values(list(df.columns)) if len(df.columns) else df\n"
        ],
        traceability=Traceability(
            sas_construct="PROC SQL", business_rule="consolida ventas"
        ),
        confidence="medium",
    )


def default_fake_caller() -> FakeCaller:
    return FakeCaller(
        {
            "analysis_reviews": fake_reviews,
            "improvements": fake_improvements,
            "matching": FileMappingBatch(mappings=[]),
            "translation": fake_translation,
        }
    )
