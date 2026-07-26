"""Entrevista inicial (Fase 1) — bloque B1-initial, deliberadamente mínimo.

Siempre las mismas tres preguntas, en el mismo orden, sin ramas (protocolo
heredado del v1 y protegido por snapshot). El nombre del proceso NO se
pregunta: se deriva del .egp en intake.json. Nunca se pregunta owner, SLA,
frecuencia ni infraestructura; las conexiones a BD se levantan en B4b solo si
el análisis encuentra evidencia.
"""

from __future__ import annotations

from pathlib import Path

from sas_migrator.core.interview._io import load_json
from sas_migrator.core.models.interview import InterviewCard, Question, QuestionType

REFERENCE_OPTIONS = ["sí (entrada y salida)", "solo entrada", "solo salida", "no", "no sé"]


def build_initial_card(state_dir: Path) -> InterviewCard:
    """La única tarjeta de la Fase 1: tres preguntas fijas de contexto."""
    intake = load_json(Path(state_dir) / "intake.json") or {}
    egp_names = [Path(str(e.get("path", ""))).name for e in intake.get("egp_files", [])]
    evidence = [f"intake.json: {', '.join(egp_names)}"] if egp_names else []

    return InterviewCard(
        card_id="B1-initial",
        interview_type="initial",
        phase=1,
        block_id="B1-initial",
        title="Contexto inicial",
        transition="Antes de analizar el código, tres preguntas de contexto:",
        questions=[
            Question(
                id="Q-001",
                text=(
                    "¿De qué trata este flujo? Descríbelo en 2-3 frases "
                    "(en términos de negocio, no técnicos)."
                ),
                evidence=evidence,
                recommended_default="no sé",
            ),
            Question(
                id="Q-002",
                text=(
                    "¿Hay consideraciones especiales que deba tener en cuenta para esta "
                    "migración? (reglas de negocio, supuestos, restricciones técnicas, "
                    "casos borde)"
                ),
                recommended_default="no",
            ),
            Question(
                id="Q-003",
                text=(
                    "¿Existen archivos de referencia de entrada y de salida para comparar "
                    "los resultados de la migración?"
                ),
                question_type=QuestionType.SINGLE_CHOICE,
                options=list(REFERENCE_OPTIONS),
                recommended_default="no sé",
            ),
        ],
    )
